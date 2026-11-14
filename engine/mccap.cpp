// mccap — движок захвата экрана MidnightClips.
//
// Забирает кадры через Desktop Duplication и отдаёт их в именованный канал ровно
// с заданной частотой (BGRA, кадр за кадром). Частоту отмеряет собственный точный
// таймер, поэтому на выходе всегда ровный поток: нет нового кадра — повторяется
// прошлый, потерян доступ к экрану (UAC, смена режима, сон монитора) — захват тихо
// пересоздаётся, а поток не прерывается.
//
// Запуск:  mccap --pipe \\.\pipe\имя --fps 60 [--x X --y Y] [--parent PID]
//          mccap --test --size 1280x720 ...   тестовая картинка вместо экрана
//          mccap --probe                       проверить, работает ли захват здесь
//          mccap --height 720 ...              уменьшить картинку (на видеокарте)
//          mccap --cpu ...                     без обработки на видеокарте: кадры BGRA как есть
// В stdout печатает одну строку «READY ширина высота fps формат», в stderr — статистику.
//
// Обычный режим: кадр экрана остаётся в памяти видеокарты, там же на него накладывается
// курсор, картинка уменьшается и переводится в NV12 — и только готовый маленький кадр
// (в 2,7 раза меньше BGRA) копируется в обычную память.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <d3dcompiler.h>
#include <mmsystem.h>
#include <objbase.h>
#include <propidl.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <initguid.h>
#include <functiondiscoverykeys_devpkey.h>

#include <algorithm>
#include <atomic>
#include <mutex>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <memory>
#include <string>
#include <vector>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "winmm.lib")
#pragma comment(lib, "d3dcompiler.lib")
#pragma comment(lib, "ole32.lib")

template <class T> static void SafeRelease(T*& p) { if (p) { p->Release(); p = nullptr; } }

static double g_freq = 0;
static double now() {
    LARGE_INTEGER c;
    if (g_freq == 0) { LARGE_INTEGER f; QueryPerformanceFrequency(&f); g_freq = (double)f.QuadPart; }
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / g_freq;
}

static void logf(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

struct Args {
    int x = 0, y = 0;
    bool hasPos = false;
    int fps = 60;
    std::wstring pipe;
    bool test = false, probe = false, cursor = true;
    int tw = 1280, th = 720;
    double seconds = 0;
    DWORD parent = 0;
    double stat = 5.0;   // как часто писать статистику, секунд
    int height = 0;      // до какой высоты уменьшать (0 — не уменьшать)
    bool cpu = false;    // не обрабатывать кадр на видеокарте
    std::wstring dump;   // --probe: сохранить один готовый кадр в файл (для проверок)
    std::wstring fakeCursor;   // проверка рисования курсора: mono / color / masked
    std::wstring audioPipe;    // канал для звука (PCM 48 кГц, стерео, 16 бит); пусто — звук не нужен
    bool audioSystem = true;   // записывать звук компьютера
    float volSystem = 1.0f, volMic = 1.0f;
    std::wstring mic;          // название микрофона; "default" — основной; пусто — без микрофона
};

static const char* kShaders = R"(
struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
VSOut vs(uint id : SV_VertexID) {
    VSOut o;
    o.uv = float2((id << 1) & 2, id & 2);
    o.pos = float4(o.uv * float2(2, -2) + float2(-1, 1), 0, 1);
    return o;
}
Texture2D tex : register(t0);
SamplerState smp : register(s0);
float3 one(float2 uv) { return tex.Sample(smp, uv).rgb; }
float3 four(float2 uv) {   // при уменьшении берём четыре точки, чтобы картинка не «сыпалась»
    float2 d = float2(ddx(uv.x), ddy(uv.y)) * 0.25;
    return (tex.Sample(smp, uv + float2(-d.x, -d.y)).rgb + tex.Sample(smp, uv + float2(d.x, -d.y)).rgb +
            tex.Sample(smp, uv + float2(-d.x, d.y)).rgb + tex.Sample(smp, uv + float2(d.x, d.y)).rgb) * 0.25;
}
float lumaOf(float3 c) { return 0.0627 + dot(c, float3(0.1826, 0.6142, 0.0620)); }           // BT.709, 16..235
float2 chromaOf(float3 c) {
    return float2(0.5020 + dot(c, float3(-0.1006, -0.3386, 0.4392)), 0.5020 + dot(c, float3(0.4392, -0.3989, -0.0403)));
}
float4 psCopy(VSOut i) : SV_Target { return tex.Sample(smp, i.uv); }
float4 psY(VSOut i) : SV_Target { return float4(lumaOf(one(i.uv)), 0, 0, 1); }
float4 psYs(VSOut i) : SV_Target { return float4(lumaOf(four(i.uv)), 0, 0, 1); }
float4 psUV(VSOut i) : SV_Target { return float4(chromaOf(one(i.uv)), 0, 1); }
float4 psUVs(VSOut i) : SV_Target { return float4(chromaOf(four(i.uv)), 0, 1); }
)";

// ------------------------------------------------------------------ точный таймер

class PreciseTimer {
    HANDLE h_ = nullptr;
public:
    bool highRes = false;
    PreciseTimer() {
        timeBeginPeriod(1);
        h_ = CreateWaitableTimerExW(nullptr, nullptr, 0x00000002 /*HIGH_RESOLUTION*/, TIMER_ALL_ACCESS);
        highRes = h_ != nullptr;
        if (!h_) h_ = CreateWaitableTimerW(nullptr, TRUE, nullptr);
    }
    void sleepUntil(double t) {
        for (;;) {
            double d = t - now();
            if (d <= 0) return;
            if (d > 0.0012 && h_) {
                LARGE_INTEGER due;
                due.QuadPart = -(LONGLONG)((d - 0.0008) * 1e7);
                if (SetWaitableTimer(h_, &due, 0, nullptr, nullptr, FALSE)) WaitForSingleObject(h_, 1000);
                else Sleep(1);
            } else {
                YieldProcessor();   // последние доли миллисекунды доводим без сна
            }
        }
    }
};

// ------------------------------------------------------------------ захват экрана

struct Capturer {
    ID3D11Device* dev = nullptr;
    ID3D11DeviceContext* ctx = nullptr;
    IDXGIOutput1* out1 = nullptr;
    IDXGIOutputDuplication* dupl = nullptr;
    ID3D11Texture2D* staging = nullptr;
    int width = 0, height = 0;
    std::vector<uint8_t> base;   // чистая картинка экрана, BGRA
    bool haveImage = false;
    std::vector<uint8_t> shape;  // курсор
    DXGI_OUTDUPL_POINTER_SHAPE_INFO shapeInfo{};
    POINT ptr{};
    bool ptrVisible = false;
    unsigned lost = 0, images = 0, pointerMoves = 0;
    double nextRetry = 0;
    HRESULT lastError = S_OK;

    // ---- обработка на видеокарте
    bool gpu = false, scaled = false, shapeDirty = false, fakePtr = false;
    int outW = 0, outH = 0, curW = 0, curH = 0;
    DXGI_FORMAT deskFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
    ID3D11Texture2D *desk = nullptr, *stage = nullptr, *yTex = nullptr, *uvTex = nullptr, *yRead = nullptr, *uvRead = nullptr;
    ID3D11Texture2D *curColor = nullptr, *curInvert = nullptr;
    ID3D11ShaderResourceView *deskSrv = nullptr, *stageSrv = nullptr, *curColorSrv = nullptr, *curInvertSrv = nullptr;
    ID3D11RenderTargetView *stageRtv = nullptr, *yRtv = nullptr, *uvRtv = nullptr;
    ID3D11VertexShader* vsFull = nullptr;
    ID3D11PixelShader *psCopy = nullptr, *psY = nullptr, *psUV = nullptr;
    ID3D11SamplerState *smpLinear = nullptr, *smpPoint = nullptr;
    ID3D11BlendState *blendAlpha = nullptr, *blendInvert = nullptr;
    ID3D11RasterizerState* raster = nullptr;

    bool makeTex(int w, int h, DXGI_FORMAT fmt, UINT bind, bool readback, ID3D11Texture2D** out) {
        D3D11_TEXTURE2D_DESC td{};
        td.Width = (UINT)w; td.Height = (UINT)h; td.MipLevels = 1; td.ArraySize = 1; td.Format = fmt;
        td.SampleDesc.Count = 1;
        td.Usage = readback ? D3D11_USAGE_STAGING : D3D11_USAGE_DEFAULT;
        td.BindFlags = readback ? 0 : bind;
        td.CPUAccessFlags = readback ? D3D11_CPU_ACCESS_READ : 0;
        return SUCCEEDED(dev->CreateTexture2D(&td, nullptr, out));
    }

    template <class T> bool compile(const char* entry, const char* target, T** shader) {
        ID3DBlob *code = nullptr, *errors = nullptr;
        HRESULT hr = D3DCompile(kShaders, strlen(kShaders), nullptr, nullptr, nullptr, entry, target, 0, 0, &code, &errors);
        if (FAILED(hr)) {
            logf("error: shader %s: %s", entry, errors ? (const char*)errors->GetBufferPointer() : "?");
            SafeRelease(errors);
            return false;
        }
        hr = createShader(code->GetBufferPointer(), code->GetBufferSize(), shader);
        SafeRelease(code);
        SafeRelease(errors);
        return SUCCEEDED(hr);
    }
    HRESULT createShader(const void* p, size_t n, ID3D11VertexShader** s) { return dev->CreateVertexShader(p, n, nullptr, s); }
    HRESULT createShader(const void* p, size_t n, ID3D11PixelShader** s) { return dev->CreatePixelShader(p, n, nullptr, s); }

    // Всё, что нужно для обработки на видеокарте. false — здесь так нельзя, остаёмся на BGRA.
    bool initGpu(int targetHeight) {
        outH = (targetHeight > 0 && targetHeight < height) ? targetHeight : height;
        outW = (int)((double)width * outH / height + 0.5);
        outW &= ~1; outH &= ~1;
        scaled = outH != (height & ~1);
        const UINT both = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        bool ok = makeTex(width, height, deskFormat, both, false, &desk)
               && makeTex(width, height, deskFormat, both, false, &stage)
               && makeTex(outW, outH, DXGI_FORMAT_R8_UNORM, D3D11_BIND_RENDER_TARGET, false, &yTex)
               && makeTex(outW / 2, outH / 2, DXGI_FORMAT_R8G8_UNORM, D3D11_BIND_RENDER_TARGET, false, &uvTex)
               && makeTex(outW, outH, DXGI_FORMAT_R8_UNORM, 0, true, &yRead)
               && makeTex(outW / 2, outH / 2, DXGI_FORMAT_R8G8_UNORM, 0, true, &uvRead);
        ok = ok && SUCCEEDED(dev->CreateShaderResourceView(desk, nullptr, &deskSrv))
                && SUCCEEDED(dev->CreateShaderResourceView(stage, nullptr, &stageSrv))
                && SUCCEEDED(dev->CreateRenderTargetView(stage, nullptr, &stageRtv))
                && SUCCEEDED(dev->CreateRenderTargetView(yTex, nullptr, &yRtv))
                && SUCCEEDED(dev->CreateRenderTargetView(uvTex, nullptr, &uvRtv));
        ok = ok && compile("vs", "vs_4_0", &vsFull) && compile("psCopy", "ps_4_0", &psCopy)
                && compile(scaled ? "psYs" : "psY", "ps_4_0", &psY) && compile(scaled ? "psUVs" : "psUV", "ps_4_0", &psUV);
        if (ok) {
            D3D11_SAMPLER_DESC sd{};
            sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
            sd.MaxLOD = D3D11_FLOAT32_MAX;
            sd.Filter = D3D11_FILTER_MIN_MAG_LINEAR_MIP_POINT;
            ok = SUCCEEDED(dev->CreateSamplerState(&sd, &smpLinear));
            sd.Filter = D3D11_FILTER_MIN_MAG_MIP_POINT;
            ok = ok && SUCCEEDED(dev->CreateSamplerState(&sd, &smpPoint));
            D3D11_BLEND_DESC bd{};
            bd.RenderTarget[0].BlendEnable = TRUE;
            bd.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
            bd.RenderTarget[0].BlendOp = bd.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
            bd.RenderTarget[0].SrcBlend = D3D11_BLEND_SRC_ALPHA;        // обычный цветной курсор
            bd.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
            bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE;
            bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA;
            ok = ok && SUCCEEDED(dev->CreateBlendState(&bd, &blendAlpha));
            bd.RenderTarget[0].SrcBlend = D3D11_BLEND_INV_DEST_COLOR;   // «инвертирующие» точки: белое = обратить цвет
            bd.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_COLOR;
            bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ZERO;
            bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
            ok = ok && SUCCEEDED(dev->CreateBlendState(&bd, &blendInvert));
            D3D11_RASTERIZER_DESC rd{};
            rd.FillMode = D3D11_FILL_SOLID;
            rd.CullMode = D3D11_CULL_NONE;
            rd.DepthClipEnable = TRUE;
            ok = ok && SUCCEEDED(dev->CreateRasterizerState(&rd, &raster));
        }
        gpu = ok;
        return ok;
    }

    // Курсор -> две текстурки: обычная (с прозрачностью) и «инвертирующая» (как у текстового курсора).
    void buildCursor() {
        shapeDirty = false;
        SafeRelease(curColorSrv); SafeRelease(curInvertSrv); SafeRelease(curColor); SafeRelease(curInvert);
        if (shape.empty()) return;
        const int cw = (int)shapeInfo.Width, pitch = (int)shapeInfo.Pitch;
        int ch = (int)shapeInfo.Height;
        const bool mono = shapeInfo.Type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME;
        const bool masked = shapeInfo.Type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR;
        if (mono) ch /= 2;
        if (cw <= 0 || ch <= 0 || (size_t)pitch * (size_t)(mono ? ch * 2 : ch) > shape.size()) return;
        std::vector<uint32_t> color((size_t)cw * ch, 0), inv((size_t)cw * ch, 0);
        bool anyColor = false, anyInv = false;
        for (int yy = 0; yy < ch; yy++) {
            for (int xx = 0; xx < cw; xx++) {
                const size_t i = (size_t)yy * cw + xx;
                if (mono) {
                    const uint8_t bit = (uint8_t)(0x80 >> (xx % 8));
                    const bool andBit = (shape[(size_t)yy * pitch + xx / 8] & bit) != 0;
                    const bool xorBit = (shape[(size_t)(yy + ch) * pitch + xx / 8] & bit) != 0;
                    if (!andBit) { color[i] = xorBit ? 0xFFFFFFFFu : 0xFF000000u; anyColor = true; }
                    else if (xorBit) { inv[i] = 0xFFFFFFFFu; anyInv = true; }
                } else {
                    uint32_t px;
                    memcpy(&px, shape.data() + (size_t)yy * pitch + (size_t)xx * 4, 4);
                    if (masked) {
                        if (px & 0xFF000000u) { if (px & 0x00FFFFFFu) { inv[i] = px | 0xFF000000u; anyInv = true; } }
                        else { color[i] = px | 0xFF000000u; anyColor = true; }
                    } else if (px & 0xFF000000u) { color[i] = px; anyColor = true; }
                }
            }
        }
        auto upload = [&](std::vector<uint32_t>& data, ID3D11Texture2D** t, ID3D11ShaderResourceView** v) {
            D3D11_TEXTURE2D_DESC td{};
            td.Width = (UINT)cw; td.Height = (UINT)ch; td.MipLevels = 1; td.ArraySize = 1;
            td.Format = DXGI_FORMAT_B8G8R8A8_UNORM; td.SampleDesc.Count = 1;
            td.Usage = D3D11_USAGE_IMMUTABLE; td.BindFlags = D3D11_BIND_SHADER_RESOURCE;
            D3D11_SUBRESOURCE_DATA init{};
            init.pSysMem = data.data();
            init.SysMemPitch = (UINT)cw * 4;
            if (SUCCEEDED(dev->CreateTexture2D(&td, &init, t))) dev->CreateShaderResourceView(*t, nullptr, v);
        };
        if (anyColor) upload(color, &curColor, &curColorSrv);
        if (anyInv) upload(inv, &curInvert, &curInvertSrv);
        curW = cw; curH = ch;
    }

    void viewport(float x, float y, float w, float h) {
        D3D11_VIEWPORT vp{};
        vp.TopLeftX = x; vp.TopLeftY = y; vp.Width = w; vp.Height = h; vp.MaxDepth = 1.0f;
        ctx->RSSetViewports(1, &vp);
    }

    // Готовый кадр NV12: курсор, уменьшение и перевод цвета — на видеокарте, копируется только результат.
    bool composeGpu(std::vector<uint8_t>& dst, bool drawCursor) {
        ID3D11ShaderResourceView* nullSrv = nullptr;
        ID3D11ShaderResourceView* src = deskSrv;
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->IASetInputLayout(nullptr);
        ctx->VSSetShader(vsFull, nullptr, 0);
        ctx->RSSetState(raster);
        if (drawCursor && ptrVisible && !shape.empty()) {
            if (shapeDirty) buildCursor();
            if (curColorSrv || curInvertSrv) {
                ctx->CopyResource(stage, desk);
                ctx->OMSetRenderTargets(1, &stageRtv, nullptr);
                viewport((float)ptr.x, (float)ptr.y, (float)curW, (float)curH);
                ctx->PSSetShader(psCopy, nullptr, 0);
                ctx->PSSetSamplers(0, 1, &smpPoint);
                if (curColorSrv) {
                    ctx->OMSetBlendState(blendAlpha, nullptr, 0xFFFFFFFF);
                    ctx->PSSetShaderResources(0, 1, &curColorSrv);
                    ctx->Draw(3, 0);
                }
                if (curInvertSrv) {
                    ctx->OMSetBlendState(blendInvert, nullptr, 0xFFFFFFFF);
                    ctx->PSSetShaderResources(0, 1, &curInvertSrv);
                    ctx->Draw(3, 0);
                }
                ctx->PSSetShaderResources(0, 1, &nullSrv);
                src = stageSrv;
            }
        }
        ctx->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
        ctx->PSSetSamplers(0, 1, &smpLinear);
        ctx->OMSetRenderTargets(1, &yRtv, nullptr);      // яркость
        viewport(0, 0, (float)outW, (float)outH);
        ctx->PSSetShader(psY, nullptr, 0);
        ctx->PSSetShaderResources(0, 1, &src);
        ctx->Draw(3, 0);
        ctx->OMSetRenderTargets(1, &uvRtv, nullptr);     // цвет, вдвое меньше
        viewport(0, 0, (float)(outW / 2), (float)(outH / 2));
        ctx->PSSetShader(psUV, nullptr, 0);
        ctx->Draw(3, 0);
        ctx->PSSetShaderResources(0, 1, &nullSrv);
        ctx->OMSetRenderTargets(0, nullptr, nullptr);
        ctx->CopyResource(yRead, yTex);
        ctx->CopyResource(uvRead, uvTex);
        D3D11_MAPPED_SUBRESOURCE m;
        if (FAILED(ctx->Map(yRead, 0, D3D11_MAP_READ, 0, &m))) return false;
        uint8_t* out = dst.data();
        for (int yy = 0; yy < outH; yy++) memcpy(out + (size_t)yy * outW, (const uint8_t*)m.pData + (size_t)yy * m.RowPitch, (size_t)outW);
        ctx->Unmap(yRead, 0);
        if (FAILED(ctx->Map(uvRead, 0, D3D11_MAP_READ, 0, &m))) return false;
        out += (size_t)outW * outH;
        for (int yy = 0; yy < outH / 2; yy++) memcpy(out + (size_t)yy * outW, (const uint8_t*)m.pData + (size_t)yy * m.RowPitch, (size_t)outW);
        ctx->Unmap(uvRead, 0);
        return true;
    }

    // Курсор-заглушка для проверок: рисование курсора должно совпадать на видеокарте и без неё.
    void fakeCursor(const std::wstring& kind) {
        const int cw = 32, ch = 32;
        ptr.x = 100; ptr.y = 100; ptrVisible = true; shapeDirty = true;
        shapeInfo.Width = cw; shapeInfo.HotSpot.x = 0; shapeInfo.HotSpot.y = 0;
        if (kind == L"mono") {
            shapeInfo.Type = DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME;
            shapeInfo.Height = ch * 2; shapeInfo.Pitch = cw / 8;
            shape.assign((size_t)(cw / 8) * ch * 2, 0);
            for (int yy = 0; yy < ch; yy++)
                for (int xx = 0; xx < cw; xx++) {
                    // четверти: чёрная, белая, прозрачная, инвертирующая
                    const bool andBit = yy >= 16, xorBit = xx >= 16;
                    if (andBit) shape[(size_t)yy * (cw / 8) + xx / 8] |= (uint8_t)(0x80 >> (xx % 8));
                    if (xorBit) shape[(size_t)(yy + ch) * (cw / 8) + xx / 8] |= (uint8_t)(0x80 >> (xx % 8));
                }
        } else {
            const bool masked = kind == L"masked";
            shapeInfo.Type = masked ? DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR : DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR;
            shapeInfo.Height = ch; shapeInfo.Pitch = cw * 4;
            shape.assign((size_t)cw * ch * 4, 0);
            for (int yy = 0; yy < ch; yy++)
                for (int xx = 0; xx < cw; xx++) {
                    uint32_t px;
                    if (masked) px = yy < 16 ? 0x0000C8FFu : (xx < 16 ? 0xFFFFFFFFu : 0xFF000000u);   // цвет / инверсия / без изменений
                    else px = yy < 16 ? 0xFFE4002Bu : (xx < 16 ? 0x80FFFFFFu : 0x00000000u);          // плотный / полупрозрачный / пусто
                    memcpy(shape.data() + ((size_t)yy * cw + xx) * 4, &px, 4);
                }
        }
    }

    bool initDevice(const Args& a) {
        IDXGIFactory1* factory = nullptr;
        HRESULT hr = CreateDXGIFactory1(__uuidof(IDXGIFactory1), (void**)&factory);
        if (FAILED(hr)) { logf("error: CreateDXGIFactory1 0x%08lx", hr); return false; }
        IDXGIAdapter1* adapter = nullptr;
        IDXGIOutput* output = nullptr;
        for (UINT i = 0; !output && factory->EnumAdapters1(i, &adapter) != DXGI_ERROR_NOT_FOUND; i++) {
            IDXGIOutput* o = nullptr;
            for (UINT j = 0; adapter->EnumOutputs(j, &o) != DXGI_ERROR_NOT_FOUND; j++) {
                DXGI_OUTPUT_DESC d;
                o->GetDesc(&d);
                bool match = a.hasPos ? (d.DesktopCoordinates.left == a.x && d.DesktopCoordinates.top == a.y)
                                      : (d.AttachedToDesktop != 0);
                if (match) { output = o; break; }
                SafeRelease(o);
            }
            if (!output) SafeRelease(adapter);
        }
        SafeRelease(factory);
        if (!output) { logf("error: monitor not found"); return false; }
        static const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0 };
        D3D_FEATURE_LEVEL got;
        hr = D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                               levels, ARRAYSIZE(levels), D3D11_SDK_VERSION, &dev, &got, &ctx);
        if (hr == E_INVALIDARG)   // система не знает уровень 11.1
            hr = D3D11CreateDevice(adapter, D3D_DRIVER_TYPE_UNKNOWN, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                   levels + 1, ARRAYSIZE(levels) - 1, D3D11_SDK_VERSION, &dev, &got, &ctx);
        SafeRelease(adapter);
        if (FAILED(hr)) { SafeRelease(output); logf("error: D3D11CreateDevice 0x%08lx", hr); return false; }
        hr = output->QueryInterface(__uuidof(IDXGIOutput1), (void**)&out1);
        SafeRelease(output);
        if (FAILED(hr)) { logf("error: IDXGIOutput1 0x%08lx", hr); return false; }
        return true;
    }

    // 1 — готово, 0 — пока не вышло (повторим позже), -1 — размер экрана изменился
    int createDupl() {
        HRESULT hr = out1->DuplicateOutput(dev, &dupl);
        if (FAILED(hr)) { lastError = hr; dupl = nullptr; return 0; }
        DXGI_OUTDUPL_DESC d;
        dupl->GetDesc(&d);
        int w = (int)d.ModeDesc.Width, h = (int)d.ModeDesc.Height;
        if (width == 0) deskFormat = d.ModeDesc.Format == DXGI_FORMAT_UNKNOWN ? DXGI_FORMAT_B8G8R8A8_UNORM : d.ModeDesc.Format;
        if (width == 0) {
            width = w; height = h;
            base.assign((size_t)w * h * 4, 0);
        } else if (w != width || h != height) {
            logf("error: screen size changed %dx%d -> %dx%d", width, height, w, h);
            return -1;
        }
        return 1;
    }

    // 1 — новая картинка, 2 — сдвинулся только курсор, 0 — ничего, -1 — нужно перезапустить движок
    int poll() {
        if (!dupl) {
            if (now() < nextRetry) return 0;
            int r = createDupl();
            if (r < 0) return -1;
            if (r == 0) { nextRetry = now() + 0.25; return 0; }
            logf("info: capture (re)created");
        }
        DXGI_OUTDUPL_FRAME_INFO info;
        IDXGIResource* res = nullptr;
        HRESULT hr = dupl->AcquireNextFrame(0, &info, &res);
        if (hr == DXGI_ERROR_WAIT_TIMEOUT) return 0;
        if (FAILED(hr)) {   // доступ потерян: UAC, смена режима, блокировка — пересоздаём
            lost++;
            lastError = hr;
            logf("info: capture lost 0x%08lx, recreating", hr);
            SafeRelease(dupl);
            nextRetry = now() + 0.1;
            return 0;
        }
        int result = 0;
        if (info.LastMouseUpdateTime.QuadPart != 0 && !fakePtr) {
            ptrVisible = info.PointerPosition.Visible != 0;
            ptr = info.PointerPosition.Position;
            pointerMoves++;
            result = 2;
            if (info.PointerShapeBufferSize > 0) {
                shape.resize(info.PointerShapeBufferSize);
                UINT need = 0;
                if (FAILED(dupl->GetFramePointerShape((UINT)shape.size(), shape.data(), &need, &shapeInfo))) shape.clear();
                shapeDirty = true;
            }
        }
        if (info.LastPresentTime.QuadPart != 0 && res) {
            ID3D11Texture2D* tex = nullptr;
            if (SUCCEEDED(res->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&tex))) {
                if (gpu) {   // картинка остаётся в памяти видеокарты
                    ctx->CopyResource(desk, tex);
                    tex->Release();
                    res->Release();
                    dupl->ReleaseFrame();
                    haveImage = true;
                    images++;
                    return 1;
                }
                if (!staging) {
                    D3D11_TEXTURE2D_DESC td;
                    tex->GetDesc(&td);
                    td.Usage = D3D11_USAGE_STAGING;
                    td.BindFlags = 0;
                    td.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
                    td.MiscFlags = 0;
                    td.MipLevels = 1;
                    td.ArraySize = 1;
                    td.SampleDesc.Count = 1;
                    td.SampleDesc.Quality = 0;
                    if (FAILED(dev->CreateTexture2D(&td, nullptr, &staging))) staging = nullptr;
                }
                if (staging) ctx->CopyResource(staging, tex);
                tex->Release();
            }
            res->Release();
            dupl->ReleaseFrame();   // отпускаем кадр как можно раньше
            D3D11_MAPPED_SUBRESOURCE m;
            if (staging && SUCCEEDED(ctx->Map(staging, 0, D3D11_MAP_READ, 0, &m))) {
                const size_t row = (size_t)width * 4;
                const uint8_t* src = (const uint8_t*)m.pData;
                for (int yy = 0; yy < height; yy++) memcpy(base.data() + (size_t)yy * row, src + (size_t)yy * m.RowPitch, row);
                ctx->Unmap(staging, 0);
                haveImage = true;
                images++;
                result = 1;
            }
            return result;
        }
        SafeRelease(res);
        dupl->ReleaseFrame();
        return result;
    }

    // Картинка экрана + курсор поверх.
    void compose(std::vector<uint8_t>& dst, bool drawCursor) {
        memcpy(dst.data(), base.data(), base.size());
        if (!drawCursor || !ptrVisible || shape.empty()) return;
        const int cw = (int)shapeInfo.Width, pitch = (int)shapeInfo.Pitch;
        int ch = (int)shapeInfo.Height;
        const bool mono = shapeInfo.Type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME;
        const bool masked = shapeInfo.Type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR;
        if (mono) ch /= 2;
        if ((size_t)pitch * (size_t)(mono ? ch * 2 : ch) > shape.size()) return;
        for (int yy = 0; yy < ch; yy++) {
            int py = ptr.y + yy;
            if (py < 0 || py >= height) continue;
            for (int xx = 0; xx < cw; xx++) {
                int px = ptr.x + xx;
                if (px < 0 || px >= width) continue;
                uint8_t* d = dst.data() + ((size_t)py * width + px) * 4;
                if (mono) {
                    const uint8_t bit = (uint8_t)(0x80 >> (xx % 8));
                    const bool andBit = (shape[(size_t)yy * pitch + xx / 8] & bit) != 0;
                    const bool xorBit = (shape[(size_t)(yy + ch) * pitch + xx / 8] & bit) != 0;
                    if (andBit) { if (xorBit) { d[0] ^= 0xFF; d[1] ^= 0xFF; d[2] ^= 0xFF; } }
                    else { d[0] = d[1] = d[2] = xorBit ? 0xFF : 0x00; }
                } else {
                    const uint8_t* s = shape.data() + (size_t)yy * pitch + (size_t)xx * 4;
                    if (masked) {
                        if (s[3]) { d[0] ^= s[0]; d[1] ^= s[1]; d[2] ^= s[2]; }
                        else { d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; }
                    } else {
                        const unsigned a = s[3];
                        if (!a) continue;
                        d[0] = (uint8_t)((s[0] * a + d[0] * (255 - a)) / 255);
                        d[1] = (uint8_t)((s[1] * a + d[1] * (255 - a)) / 255);
                        d[2] = (uint8_t)((s[2] * a + d[2] * (255 - a)) / 255);
                    }
                }
            }
        }
    }

    void close() {
        SafeRelease(curColorSrv); SafeRelease(curInvertSrv); SafeRelease(curColor); SafeRelease(curInvert);
        SafeRelease(deskSrv); SafeRelease(stageSrv); SafeRelease(stageRtv); SafeRelease(yRtv); SafeRelease(uvRtv);
        SafeRelease(desk); SafeRelease(stage); SafeRelease(yTex); SafeRelease(uvTex); SafeRelease(yRead); SafeRelease(uvRead);
        SafeRelease(vsFull); SafeRelease(psCopy); SafeRelease(psY); SafeRelease(psUV);
        SafeRelease(smpLinear); SafeRelease(smpPoint); SafeRelease(blendAlpha); SafeRelease(blendInvert); SafeRelease(raster);
        SafeRelease(staging); SafeRelease(dupl); SafeRelease(out1); SafeRelease(ctx); SafeRelease(dev);
    }
};

// ------------------------------------------------------------------ тестовая картинка

static long long epochMicros() {
    FILETIME ft;
    GetSystemTimePreciseAsFileTime(&ft);
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return (long long)((u.QuadPart - 116444736000000000ULL) / 10ULL);
}

static void drawTest(std::vector<uint8_t>& f, int w, int h, double t) {
    static const uint32_t bars[7] = { 0xFFF4F4F4, 0xFFFFE100, 0xFF20D6E8, 0xFF2FCB4B, 0xFFE63BC8, 0xFFE4002B, 0xFF2743D8 };
    uint32_t* p = (uint32_t*)f.data();
    for (int x = 0; x < w; x++) p[x] = bars[(x * 7) / w];
    for (int y = 1; y < h; y++) memcpy(p + (size_t)y * w, p, (size_t)w * 4);
    const int bw = std::max(8, w / 40);
    const int bx = (int)(t * w * 0.5) % (w - bw);   // полоса проходит экран за 2 секунды
    for (int y = h / 3; y < 2 * h / 3; y++)
        for (int x = bx; x < bx + bw; x++) p[(size_t)y * w + x] = 0xFF000000;
}

// ------------------------------------------------------------------ очередь кадров
//
// Кадры снимаются строго по своим часам и кладутся в очередь, а в канал их пишет
// отдельный поток. ffmpeg читает видео не равномерно, а порциями — в такт звуку;
// без очереди пришлось бы отдавать ему пачку одинаковых кадров.

typedef std::shared_ptr<std::vector<uint8_t>> FramePtr;

struct FrameQueue {
    CRITICAL_SECTION cs;
    CONDITION_VARIABLE cv;
    std::deque<FramePtr> q;
    std::vector<std::vector<uint8_t>*> pool;
    size_t frameSize = 0, capacity = 8;
    bool stop = false;
    long long overflow = 0;
    void (*onDrop)() = nullptr;

    FrameQueue() { InitializeCriticalSection(&cs); InitializeConditionVariable(&cv); }

    FramePtr acquire() {   // свободный буфер под новый кадр (буферы переиспользуются)
        std::vector<uint8_t>* v = nullptr;
        EnterCriticalSection(&cs);
        if (!pool.empty()) { v = pool.back(); pool.pop_back(); }
        LeaveCriticalSection(&cs);
        if (!v) v = new std::vector<uint8_t>(frameSize);
        return FramePtr(v, [this](std::vector<uint8_t>* p) {
            EnterCriticalSection(&cs);
            pool.push_back(p);
            LeaveCriticalSection(&cs);
        });
    }
    void push(const FramePtr& f) {
        EnterCriticalSection(&cs);
        if (q.size() >= capacity) {   // читатель совсем встал — старое не копим
            q.pop_front();
            overflow++;
            if (onDrop) onDrop();     // столько же выбросить из звука, чтобы он не разошёлся с картинкой
        }
        q.push_back(f);
        LeaveCriticalSection(&cs);
        WakeConditionVariable(&cv);
    }
    size_t depth() {
        EnterCriticalSection(&cs);
        size_t d = q.size();
        LeaveCriticalSection(&cs);
        return d;
    }
    void trim(size_t count) {
        EnterCriticalSection(&cs);
        while (count-- && !q.empty()) q.pop_front();
        LeaveCriticalSection(&cs);
    }
    bool pop(FramePtr& out) {
        EnterCriticalSection(&cs);
        while (q.empty() && !stop) SleepConditionVariableCS(&cv, &cs, 200);
        bool ok = !q.empty();
        if (ok) { out = q.front(); q.pop_front(); }
        LeaveCriticalSection(&cs);
        return ok;
    }
    void finish() {
        EnterCriticalSection(&cs);
        stop = true;
        LeaveCriticalSection(&cs);
        WakeAllConditionVariable(&cv);
    }
};

static FrameQueue g_queue;
static HANDLE g_pipe = INVALID_HANDLE_VALUE;
static volatile LONG g_writerFailed = 0;
static volatile LONG64 g_written = 0;
static volatile LONG g_writeMaxMicros = 0, g_writeMaxTotalMicros = 0;

static bool writeAll(HANDLE h, const uint8_t* data, size_t size);

static DWORD WINAPI writerThread(LPVOID) {
    FramePtr item;
    while (g_queue.pop(item)) {
        const double w0 = now();
        if (!writeAll(g_pipe, item->data(), item->size())) { InterlockedExchange(&g_writerFailed, 1); break; }
        const LONG us = (LONG)((now() - w0) * 1e6);
        if (us > g_writeMaxMicros) InterlockedExchange(&g_writeMaxMicros, us);
        if (us > g_writeMaxTotalMicros) InterlockedExchange(&g_writeMaxTotalMicros, us);
        InterlockedIncrement64(&g_written);
        item.reset();
    }
    return 0;
}

// ------------------------------------------------------------------ звук
//
// Звук компьютера и микрофон снимаются здесь же и привязаны к тем же часам, что и кадры:
// отсчёт номер N в звуке — это ровно момент t0 + N/48000, как кадр K — момент t0 + K/fps.
// Поэтому звук и картинка в клипе совпадают независимо от того, как их читает ffmpeg.

static const int kRate = 48000;
static const double kAudioDelay = 0.08;   // звук отдаём с запасом 80 мс: за это время он точно успевает прийти
static const GUID kFloatFormat = { 0x00000003, 0x0000, 0x0010, { 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71 } };
static double g_t0 = 0;                    // начало общего отсчёта (0 — ещё не начался)
static std::atomic<long long> g_audioSkip(0);
static int g_fps = 60;
static void onVideoDrop() { g_audioSkip += kRate / g_fps; }

struct AudioSource {
    bool loopback = false;
    std::wstring name;         // для микрофона: название устройства или "default"
    float gain = 1.0f;
    IMMDevice* dev = nullptr;
    IAudioClient* client = nullptr;
    IAudioCaptureClient* cap = nullptr;
    std::deque<float> fifo;    // стерео, по два числа на отсчёт
    long long frontPos = 0;    // какому моменту (в отсчётах от t0) соответствует начало очереди
    double nextOpen = 0;
    bool reported = false;
    long long captured = 0, reopened = 0;
    HRESULT openError = S_OK;

    void close() { SafeRelease(cap); SafeRelease(client); SafeRelease(dev); }

    bool open() {
        IMMDeviceEnumerator* en = nullptr;
        if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en)))
            return false;
        HRESULT hr = E_FAIL;
        if (loopback) hr = en->GetDefaultAudioEndpoint(eRender, eConsole, &dev);
        else if (name.empty() || name == L"default") hr = en->GetDefaultAudioEndpoint(eCapture, eConsole, &dev);
        else {
            IMMDeviceCollection* all = nullptr;
            if (SUCCEEDED(en->EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE, &all))) {
                UINT count = 0;
                all->GetCount(&count);
                for (UINT i = 0; i < count && !dev; i++) {
                    IMMDevice* d = nullptr;
                    IPropertyStore* props = nullptr;
                    if (FAILED(all->Item(i, &d))) continue;
                    if (SUCCEEDED(d->OpenPropertyStore(STGM_READ, &props))) {
                        PROPVARIANT v;
                        PropVariantInit(&v);
                        if (SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName, &v)) && v.vt == VT_LPWSTR) {
                            const std::wstring friendly = v.pwszVal;   // названия могут быть обрезаны — сравниваем по началу
                            const size_t n = std::min(friendly.size(), name.size());
                            if (n >= 4 && friendly.compare(0, n, name, 0, n) == 0) { dev = d; d = nullptr; }
                        }
                        PropVariantClear(&v);
                        props->Release();
                    }
                    SafeRelease(d);
                }
                all->Release();
            }
            hr = dev ? S_OK : E_FAIL;
        }
        en->Release();
        if (FAILED(hr) || !dev) { dev = nullptr; return false; }
        if (FAILED(dev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&client))) { close(); return false; }
        WAVEFORMATEXTENSIBLE wf{};   // просим у Windows сразу 48 кГц, стерео — пересчёт она делает сама
        wf.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
        wf.Format.nChannels = 2;
        wf.Format.nSamplesPerSec = kRate;
        wf.Format.wBitsPerSample = 32;
        wf.Format.nBlockAlign = 8;
        wf.Format.nAvgBytesPerSec = kRate * 8;
        wf.Format.cbSize = 22;
        wf.Samples.wValidBitsPerSample = 32;
        wf.dwChannelMask = 3;   // левый + правый
        wf.SubFormat = kFloatFormat;
        const DWORD flags = 0x80000000 /*AUTOCONVERTPCM*/ | 0x08000000 /*SRC_DEFAULT_QUALITY*/
                          | (loopback ? AUDCLNT_STREAMFLAGS_LOOPBACK : 0);
        hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2000000 /*200 мс*/, 0, (WAVEFORMATEX*)&wf, nullptr);
        if (SUCCEEDED(hr)) hr = client->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
        if (SUCCEEDED(hr)) hr = client->Start();
        if (FAILED(hr)) { openError = hr; close(); return false; }
        // Некоторые устройства (виртуальные кабели) при старте отдают остатки старого звука — выбрасываем их.
        Sleep(60);
        UINT32 stale = 0;
        while (SUCCEEDED(cap->GetNextPacketSize(&stale)) && stale > 0) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            if (FAILED(cap->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            cap->ReleaseBuffer(frames);
        }
        return true;
    }

    // Забрать всё, что пришло с устройства, в очередь.
    void poll() {
        if (!cap) {
            if (now() < nextOpen) return;
            nextOpen = now() + 1.0;
            openError = S_OK;
            if (!open()) {
                if (!reported) {   // 0x80070005 — Windows запретила доступ (настройки конфиденциальности)
                    logf("info: audio %s unavailable 0x%08lx", loopback ? "system" : "mic", openError);
                    reported = true;
                }
                return;
            }
            reopened++;
            logf("info: audio %s opened", loopback ? "system" : "mic");
            reported = false;
        }
        UINT32 packet = 0;
        HRESULT hr;
        while (SUCCEEDED(hr = cap->GetNextPacketSize(&packet)) && packet > 0) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            hr = cap->GetBuffer(&data, &frames, &flags, nullptr, nullptr);
            if (FAILED(hr)) break;
            push((const float*)data, frames, (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0);
            cap->ReleaseBuffer(frames);
        }
        if (FAILED(hr)) {   // устройство отключили или сменили — откроем заново
            logf("info: audio %s lost 0x%08lx", loopback ? "system" : "mic", hr);
            close();
            nextOpen = now() + 0.5;
        }
    }

    void push(const float* data, UINT32 frames, bool silent) {
        if (g_t0 == 0 || frames == 0) return;
        const long long start = (long long)((now() - g_t0) * kRate) - frames;   // момент начала этой порции
        const long long have = (long long)(fifo.size() / 2);
        if (have == 0) frontPos = start;
        else {
            const long long gap = start - (frontPos + have);
            if (gap > kRate * 6 / 100) fifo.insert(fifo.end(), (size_t)std::min<long long>(gap, kRate) * 2, 0.0f);   // устройство молчало
        }
        if (silent) fifo.insert(fifo.end(), (size_t)frames * 2, 0.0f);
        else fifo.insert(fifo.end(), data, data + (size_t)frames * 2);
        captured += frames;
        const long long extra = (long long)(fifo.size() / 2) - kRate * 2;   // больше двух секунд не копим
        if (extra > 0) { fifo.erase(fifo.begin(), fifo.begin() + (size_t)extra * 2); frontPos += extra; }
    }

    // Подмешать в out отсчёты [pos, pos + count).
    void mixInto(std::vector<float>& out, long long pos, long long count) {
        long long have = (long long)(fifo.size() / 2);
        if (have && frontPos < pos) {   // устаревшее
            const long long drop = std::min(have, pos - frontPos);
            fifo.erase(fifo.begin(), fifo.begin() + (size_t)drop * 2);
            frontPos += drop;
            have -= drop;
        }
        if (!have || frontPos >= pos + count) return;
        const long long offset = frontPos - pos;
        const long long n = std::min(have, count - offset);
        for (long long i = 0; i < n * 2; i++) out[(size_t)(offset * 2 + i)] += fifo[(size_t)i] * gain;
        fifo.erase(fifo.begin(), fifo.begin() + (size_t)n * 2);
        frontPos += n;
    }
};

static HANDLE g_audioPipe = INVALID_HANDLE_VALUE;
static std::vector<AudioSource*> g_sources;
static volatile LONG g_audioStop = 0, g_audioFailed = 0;
static volatile LONG64 g_audioWritten = 0;

static DWORD WINAPI audioThread(LPVOID) {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
    if (!ConnectNamedPipe(g_audioPipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
        logf("error: audio pipe %lu", GetLastError());
        InterlockedExchange(&g_audioFailed, 1);
        return 0;
    }
    long long outPos = 0;
    std::vector<float> mix;
    std::vector<int16_t> pcm;
    while (!g_audioStop) {
        for (AudioSource* s : g_sources) s->poll();
        if (g_t0 != 0) {
            outPos += g_audioSkip.exchange(0);   // из видео выбросили кадры — выбрасываем столько же звука
            const long long target = (long long)((now() - g_t0 - kAudioDelay) * kRate);
            const long long due = std::min<long long>(target - outPos, kRate);
            if (due > 0) {
                mix.assign((size_t)due * 2, 0.0f);
                for (AudioSource* s : g_sources) s->mixInto(mix, outPos, due);
                pcm.resize((size_t)due * 2);
                for (size_t i = 0; i < pcm.size(); i++) {
                    float v = mix[i] * 32767.0f;
                    pcm[i] = (int16_t)(v > 32767.0f ? 32767.0f : (v < -32768.0f ? -32768.0f : v));
                }
                if (!writeAll(g_audioPipe, (const uint8_t*)pcm.data(), pcm.size() * 2)) {
                    InterlockedExchange(&g_audioFailed, 1);   // читатель (ffmpeg) закрылся
                    break;
                }
                outPos += due;
                InterlockedAdd64(&g_audioWritten, due);
            }
        }
        Sleep(5);
    }
    for (AudioSource* s : g_sources) s->close();
    CoUninitialize();
    return 0;
}

// ------------------------------------------------------------------ запуск

static DWORD WINAPI parentWatch(LPVOID param) {
    HANDLE h = OpenProcess(SYNCHRONIZE, FALSE, (DWORD)(ULONG_PTR)param);
    if (h) { WaitForSingleObject(h, INFINITE); ExitProcess(0); }
    return 0;
}

static bool writeAll(HANDLE h, const uint8_t* data, size_t size) {
    while (size) {
        DWORD done = 0;
        DWORD chunk = (DWORD)std::min<size_t>(size, 4u << 20);
        if (!WriteFile(h, data, chunk, &done, nullptr) || !done) return false;
        data += done;
        size -= done;
    }
    return true;
}

int wmain(int argc, wchar_t** argv) {
    Args a;
    for (int i = 1; i < argc; i++) {
        std::wstring k = argv[i];
        auto next = [&]() -> const wchar_t* { return i + 1 < argc ? argv[++i] : L""; };
        if (k == L"--pipe") a.pipe = next();
        else if (k == L"--fps") a.fps = _wtoi(next());
        else if (k == L"--x") { a.x = _wtoi(next()); a.hasPos = true; }
        else if (k == L"--y") { a.y = _wtoi(next()); a.hasPos = true; }
        else if (k == L"--parent") a.parent = (DWORD)_wtoi(next());
        else if (k == L"--seconds") a.seconds = _wtof(next());
        else if (k == L"--stat") a.stat = std::max(0.5, _wtof(next()));
        else if (k == L"--size") swscanf_s(next(), L"%dx%d", &a.tw, &a.th);
        else if (k == L"--test") a.test = true;
        else if (k == L"--probe") a.probe = true;
        else if (k == L"--no-cursor") a.cursor = false;
        else if (k == L"--height") a.height = _wtoi(next());
        else if (k == L"--cpu") a.cpu = true;
        else if (k == L"--dump") a.dump = next();
        else if (k == L"--fake-cursor") a.fakeCursor = next();
        else if (k == L"--audio-pipe") a.audioPipe = next();
        else if (k == L"--audio-system") a.audioSystem = _wtoi(next()) != 0;
        else if (k == L"--vol-system") a.volSystem = (float)_wtof(next());
        else if (k == L"--vol-mic") a.volMic = (float)_wtof(next());
        else if (k == L"--mic") a.mic = next();
    }
    a.fps = std::max(1, std::min(480, a.fps));
    if (a.parent) CreateThread(nullptr, 0, parentWatch, (LPVOID)(ULONG_PTR)a.parent, 0, nullptr);
    SetPriorityClass(GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);

    PreciseTimer timer;
    Capturer cap;
    int w = a.tw, h = a.th;

    if (!a.test) {
        if (!cap.initDevice(a)) return 2;
        double until = now() + 3.0;
        int r = 0;
        while ((r = cap.createDupl()) == 0 && now() < until) Sleep(50);
        if (r <= 0) { logf("error: DuplicateOutput 0x%08lx", cap.lastError); return 2; }
        if (!a.cpu && !cap.initGpu(a.height)) {
            logf("error: gpu path unavailable");   // программа запустит движок заново с --cpu
            return 5;
        }
        if (!a.fakeCursor.empty()) { cap.fakePtr = true; cap.fakeCursor(a.fakeCursor); }
        until = now() + 3.0;   // ждём первую картинку экрана
        while (!cap.haveImage && now() < until) {
            r = cap.poll();
            if (r < 0) return 3;
            if (!cap.haveImage) Sleep(5);
        }
        w = cap.gpu ? cap.outW : cap.width;
        h = cap.gpu ? cap.outH : cap.height;
    }
    const bool nv12 = !a.test && cap.gpu;
    const size_t frameSize = nv12 ? (size_t)w * h * 3 / 2 : (size_t)w * h * 4;

    if (a.probe) {
        const double t0 = now();
        long n = 0;
        while (now() - t0 < (a.seconds > 0 ? a.seconds : 3.0)) {
            timer.sleepUntil(t0 + (double)n / a.fps);
            if (cap.poll() < 0) break;
            n++;
        }
        bool dumped = false;
        if (!a.dump.empty()) {   // один готовый кадр в файл — сверить картинку в проверках
            std::vector<uint8_t> frame(frameSize, 0);
            const bool ok = nv12 ? cap.composeGpu(frame, a.cursor) : (cap.compose(frame, a.cursor), true);
            FILE* f = nullptr;
            if (ok && _wfopen_s(&f, a.dump.c_str(), L"wb") == 0 && f) {
                dumped = fwrite(frame.data(), 1, frame.size(), f) == frame.size();
                fclose(f);
            }
        }
        printf("PROBE ok %dx%d %s source=%dx%d images=%u pointer=%u lost=%u highres_timer=%d dumped=%d\n",
               w, h, nv12 ? "nv12" : "bgra", cap.width, cap.height, cap.images, cap.pointerMoves, cap.lost,
               timer.highRes ? 1 : 0, dumped ? 1 : 0);
        fflush(stdout);
        cap.close();
        return 0;
    }

    if (a.pipe.empty()) { logf("error: --pipe required"); return 2; }
    g_queue.frameSize = frameSize;
    g_queue.capacity = (size_t)std::max(8, (int)(a.fps * 0.25));   // до четверти секунды кадров
    HANDLE pipe = CreateNamedPipeW(a.pipe.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1,
                                   (DWORD)std::min<size_t>(frameSize, 8u << 20), 0, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) { logf("error: CreateNamedPipe %lu", GetLastError()); return 2; }
    g_pipe = pipe;

    if (!a.audioPipe.empty()) {
        g_audioPipe = CreateNamedPipeW(a.audioPipe.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1, 1 << 16, 0, 0, nullptr);
        if (g_audioPipe == INVALID_HANDLE_VALUE) { logf("error: CreateNamedPipe(audio) %lu", GetLastError()); return 2; }
    }
    printf("READY %d %d %d %s\n", w, h, a.fps, nv12 ? "nv12" : "bgra");
    fflush(stdout);

    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
        logf("error: ConnectNamedPipe %lu", GetLastError());
        return 2;
    }
    HANDLE writer = CreateThread(nullptr, 0, writerThread, nullptr, 0, nullptr);

    const bool audio = !a.audioPipe.empty();
    HANDLE audioWorker = nullptr;
    if (audio) {
        g_fps = a.fps;
        g_queue.onDrop = onVideoDrop;
        if (a.audioSystem) { AudioSource* s = new AudioSource(); s->loopback = true; s->gain = a.volSystem; g_sources.push_back(s); }
        if (!a.mic.empty()) { AudioSource* s = new AudioSource(); s->name = a.mic; s->gain = a.volMic; g_sources.push_back(s); }
        audioWorker = CreateThread(nullptr, 0, audioThread, nullptr, 0, nullptr);
    }

    const double t0 = now();
    g_t0 = t0;   // с этого момента идёт общий отсчёт кадров и звука
    long long n = 0, produced = 0, trimmed = 0, lateOver2 = 0;
    FramePtr cur;
    double statT = t0, maxLate = 0, totalMaxLate = 0;
    long long statWritten = 0, statTrimmed = 0, statOverflow = 0;
    unsigned statImages = 0;
    size_t minDepth = (size_t)-1, maxDepth = 0;
    int windowCount = 0;
    int code = 0;
    for (;;) {
        const double due = t0 + (double)n / a.fps;
        timer.sleepUntil(due);
        const double late = now() - due;   // насколько опоздали к своей отметке
        if (late > maxLate) maxLate = late;
        if (late > totalMaxLate) totalMaxLate = late;
        if (late > 0.002) lateOver2++;
        if (late > 0.25) n = (long long)((now() - t0) * a.fps);   // система подвисала — не догоняем пачкой

        if (a.test) {
            cur = g_queue.acquire();
            drawTest(*cur, w, h, now() - t0);
            const long long stamp[2] = { produced, epochMicros() };   // номер и время кадра — для проверок
            memcpy(cur->data(), stamp, sizeof(stamp));
        } else {
            int r = cap.poll();
            if (r < 0) { code = 3; break; }
            if (r > 0 || !cur) {
                cur = g_queue.acquire();
                if (nv12) {
                    if (!cap.composeGpu(*cur, a.cursor)) { logf("error: gpu readback failed"); code = 5; break; }
                } else {
                    cap.compose(*cur, a.cursor);
                }
            }
        }

        // Очередь должна быть почти пустой: кадр уходит к ffmpeg сразу, как тот готов его взять.
        // Короткие всплески — норма (ffmpeg читает порциями). А если очередь не опускается целую
        // секунду, значит, накопилось отставание картинки от звука — лишнее выбрасываем.
        const size_t depth = g_queue.depth();
        if (depth < minDepth) minDepth = depth;
        if (depth > maxDepth) maxDepth = depth;
        if (++windowCount >= a.fps) {
            // Со своим звуком очередь подрезать нельзя (и незачем): звук идёт с той же задержкой.
            if (minDepth > 2 && !audio) { g_queue.trim(minDepth - 1); trimmed += (long long)(minDepth - 1); }
            minDepth = (size_t)-1;
            windowCount = 0;
        }
        g_queue.push(cur);   // нового кадра нет — повторяется прошлый, без копирования
        n++;
        produced++;
        if (g_writerFailed || g_audioFailed) break;   // читатель (ffmpeg) закрылся

        const double t = now();
        if (t - statT >= a.stat) {
            const long long written = g_written;
            const LONG wmax = InterlockedExchange(&g_writeMaxMicros, 0);
            if (audio) {
                std::string info;
                for (AudioSource* s : g_sources) {
                    char part[96];
                    snprintf(part, sizeof(part), " %s=%s/%lld", s->loopback ? "system" : "mic", s->cap ? "on" : "off", s->captured);
                    info += part;
                }
                logf("audio written=%.1fs%s", (double)g_audioWritten / kRate, info.c_str());
            }
            logf("stat out=%.1f new=%.1f late_max=%.2fms write_max=%.1fms queue_max=%u trimmed=%lld overflow=%lld lost=%u",
                 (double)(written - statWritten) / (t - statT), (double)(cap.images - statImages) / (t - statT),
                 maxLate * 1000.0, wmax / 1000.0, (unsigned)maxDepth, trimmed - statTrimmed,
                 g_queue.overflow - statOverflow, cap.lost);
            statT = t; statWritten = written; statImages = cap.images; statTrimmed = trimmed;
            statOverflow = g_queue.overflow; maxLate = 0; maxDepth = 0;
        }
        if (a.seconds > 0 && t - t0 >= a.seconds) break;
    }
    cur.reset();
    g_queue.finish();   // писатель дописывает остаток очереди и выходит
    if (writer) WaitForSingleObject(writer, 3000);
    if (audioWorker) {
        // дать звуку дойти до той же отметки, что и видео
        const double until = now() + kAudioDelay + 0.05;
        while (now() < until && !g_audioFailed) Sleep(5);
        InterlockedExchange(&g_audioStop, 1);
        WaitForSingleObject(audioWorker, 2000);
        if (g_audioPipe != INVALID_HANDLE_VALUE) { FlushFileBuffers(g_audioPipe); CloseHandle(g_audioPipe); }
    }
    logf("done frames=%lld elapsed=%.3f late_max=%.2fms late_over_2ms=%lld write_max=%.1fms highres_timer=%d trimmed=%lld overflow=%lld",
         (long long)g_written, now() - t0, totalMaxLate * 1000.0, lateOver2, g_writeMaxTotalMicros / 1000.0,
         timer.highRes ? 1 : 0, trimmed, g_queue.overflow);
    FlushFileBuffers(pipe);
    CloseHandle(pipe);
    cap.close();
    return code;
}

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
//          mccap --encode h264_nvenc --bitrate 12 ...   кодировать прямо здесь (нужна сборка с FFmpeg)
// В режиме --encode в канал идёт уже сжатое видео (H.264 в контейнере NUT), а кадр экрана вообще
// не покидает видеокарту: захват -> курсор и уменьшение -> кодировщик, всё в её памяти.
//
// Обычный режим: кадр экрана остаётся в памяти видеокарты, там же на него накладывается
// курсор, картинка уменьшается и переводится в NV12 — и только готовый маленький кадр
// (в 2,7 раза меньше BGRA) копируется в обычную память.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_6.h>
#include <d3dcompiler.h>
#include <mmsystem.h>
#include <objbase.h>
#include <propidl.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <initguid.h>
#include <functiondiscoverykeys_devpkey.h>
#include <audiopolicy.h>
#include <audioclientactivationparams.h>
#include <tlhelp32.h>
#include <winrt/base.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>

#include <algorithm>
#include <atomic>
#include <mutex>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cwctype>
#include <deque>
#include <map>
#include <set>
#include <memory>
#include <string>
#include <vector>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "winmm.lib")
#pragma comment(lib, "d3dcompiler.lib")
#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "mmdevapi.lib")
#pragma comment(lib, "windowsapp.lib")
#pragma comment(lib, "user32.lib")

#ifdef MC_FFMPEG   // кодирование прямо в движке — через библиотеки FFmpeg
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/avutil.h>
#include <libavutil/opt.h>
#include <libavutil/channel_layout.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_d3d11va.h>
}
#pragma comment(lib, "avcodec.lib")
#pragma comment(lib, "avformat.lib")
#pragma comment(lib, "avutil.lib")
#endif

namespace wgc = winrt::Windows::Graphics::Capture;
namespace wgd = winrt::Windows::Graphics::DirectX;

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
    std::map<std::wstring, float> appVol;   // своя громкость программ: имя exe (строчными) -> громкость
    // Отдельные звуковые дорожки: кроме общей (всё вместе) — ещё tracks штук, каналы «<audio-pipe>_1», «_2»...
    int tracks = 0, trackMic = 0, trackOther = 0;
    std::map<std::wstring, int> appTrack;   // программа -> номер дорожки (1..tracks)
    bool includeParent = false;             // записывать и звуки запустившей нас программы (сигналы MidnightClips)
    bool listApps = false;     // вывести программы, у которых сейчас есть звук, и выйти
    HWND window = nullptr;     // записывать одно окно (а не весь монитор)
    int frameW = 0, frameH = 0; // размер кадра на выходе задан снаружи (окно вписывается в него с полями)
    std::wstring encode;       // кодировать здесь же: h264_nvenc / h264_amf / h264_qsv / libx264
    int bitrate = 12;          // Мбит/с
    bool mux = false;          // (с --encode) упаковывать самим: в канал идёт готовый поток MPEG-TS, ffmpeg не нужен
    bool muxAudio = false;     // ... и кодировать в него звук (AAC)
    int audioOffset = 0;       // сдвиг звука относительно картинки, мс
    std::wstring frames;       // gpu — кадр остаётся на видеокарте; cpu — через обычную память;
                               // download — как gpu, но кадр скачивается перед кодированием (для проверок)
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
// Подготовка картинки экрана: повёрнутый монитор — поставить «как на столе», HDR — привести к обычным цветам.
cbuffer Prep : register(b0) { float4 prm; }   // x: поворот 0..3 (по 90 градусов), y: 1 = HDR, z: яркость белого
float4 psPrep(VSOut i) : SV_Target {
    float2 uv = i.uv;
    if (prm.x > 2.5) uv = float2(1 - i.uv.y, i.uv.x);
    else if (prm.x > 1.5) uv = float2(1 - i.uv.x, 1 - i.uv.y);
    else if (prm.x > 0.5) uv = float2(i.uv.y, 1 - i.uv.x);
    float3 c = tex.Sample(smp, uv).rgb;
    if (prm.y > 0.5) {
        c = saturate(c / prm.z);   // обычный белый = 1; всё, что ярче, обрезается
        float3 lo = c * 12.92, hi = 1.055 * pow(max(c, 1e-6), 1.0 / 2.4) - 0.055;
        c = lerp(lo, hi, step(0.0031308, c));
    }
    return float4(c, 1);
}
float4 psCopy4(VSOut i) : SV_Target { return float4(four(i.uv), 1); }
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

    // ---- захват одного окна (Windows Graphics Capture)
    bool windowMode = false;
    HWND hwndTarget = nullptr;
    std::atomic<bool> windowClosed{false};
    int srcW = 0, srcH = 0;   // текущий размер окна (может меняться; размер кадра на выходе остаётся прежним)
    wgc::GraphicsCaptureItem item{nullptr};
    wgc::Direct3D11CaptureFramePool pool{nullptr};
    wgc::GraphicsCaptureSession session{nullptr};
    wgd::Direct3D11::IDirect3DDevice rtDevice{nullptr};

    // ---- обработка на видеокарте
    bool gpu = false, scaled = false, shapeDirty = false, fakePtr = false;
    // Источник: «сырой» кадр экрана может быть повёрнут (монитор стоит боком) или в формате HDR —
    // тогда он сначала приводится к обычному виду (raw -> desk).
    bool wantGpu = false, prepass = false, hdr = false;
    int frameW = 0, frameH = 0;
    int rot = 0, rawW = 0, rawH = 0;
    float white = 1.0f;
    DXGI_FORMAT rawFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
    std::wstring deviceName;
    ID3D11Texture2D* raw = nullptr;
    ID3D11ShaderResourceView* rawSrv = nullptr;
    ID3D11RenderTargetView* deskRtv = nullptr;
    ID3D11PixelShader* psPrep = nullptr;
    ID3D11Buffer* prepBuf = nullptr;
    int outW = 0, outH = 0, curW = 0, curH = 0;
    DXGI_FORMAT deskFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
    ID3D11Texture2D *desk = nullptr, *stage = nullptr, *yTex = nullptr, *uvTex = nullptr, *yRead = nullptr, *uvRead = nullptr;
    ID3D11Texture2D *curColor = nullptr, *curInvert = nullptr;
    ID3D11ShaderResourceView *deskSrv = nullptr, *stageSrv = nullptr, *curColorSrv = nullptr, *curInvertSrv = nullptr;
    ID3D11RenderTargetView *stageRtv = nullptr, *yRtv = nullptr, *uvRtv = nullptr;
    ID3D11VertexShader* vsFull = nullptr;
    ID3D11PixelShader *psCopy = nullptr, *psY = nullptr, *psUV = nullptr, *psOut = nullptr;
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
        if (frameW >= 16 && frameH >= 16) {   // кадр как у монитора: при переключении «окно <-> монитор» буфер остаётся цел
            outW = frameW & ~1; outH = frameH & ~1;
            scaled = true;
        }
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
                && SUCCEEDED(dev->CreateRenderTargetView(desk, nullptr, &deskRtv))
                && SUCCEEDED(dev->CreateRenderTargetView(yTex, nullptr, &yRtv))
                && SUCCEEDED(dev->CreateRenderTargetView(uvTex, nullptr, &uvRtv));
        ok = ok && compile("vs", "vs_4_0", &vsFull) && compile("psCopy", "ps_4_0", &psCopy)
                && compile(scaled ? "psYs" : "psY", "ps_4_0", &psY) && compile(scaled ? "psUVs" : "psUV", "ps_4_0", &psUV)
                && compile(scaled ? "psCopy4" : "psCopy", "ps_4_0", &psOut) && compile("psPrep", "ps_4_0", &psPrep);
        if (ok) {
            D3D11_BUFFER_DESC cb{};
            cb.ByteWidth = 16;
            cb.Usage = D3D11_USAGE_DEFAULT;
            cb.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
            ok = SUCCEEDED(dev->CreateBuffer(&cb, nullptr, &prepBuf)) && makeRaw();
        }
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

    // Текстура под «сырой» кадр экрана — нужна, только если его приходится поворачивать или переводить из HDR.
    bool makeRaw() {
        SafeRelease(rawSrv); SafeRelease(raw);
        if (!prepass) return true;
        return makeTex(rawW, rawH, rawFormat, D3D11_BIND_SHADER_RESOURCE, false, &raw)
            && SUCCEEDED(dev->CreateShaderResourceView(raw, nullptr, &rawSrv));
    }

    // raw -> desk: поворот и/или перевод HDR в обычные цвета.
    void runPrepass() {
        const float prm[4] = { (float)rot, hdr ? 1.0f : 0.0f, white, 0 };
        ID3D11ShaderResourceView* nullSrv = nullptr;
        ctx->UpdateSubresource(prepBuf, 0, nullptr, prm, 0, 0);
        ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        ctx->IASetInputLayout(nullptr);
        ctx->VSSetShader(vsFull, nullptr, 0);
        ctx->RSSetState(raster);
        ctx->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
        ctx->OMSetRenderTargets(1, &deskRtv, nullptr);
        viewport(0, 0, (float)srcW, (float)srcH);
        ctx->PSSetShader(psPrep, nullptr, 0);
        ctx->PSSetConstantBuffers(0, 1, &prepBuf);
        ctx->PSSetSamplers(0, 1, &smpPoint);
        ctx->PSSetShaderResources(0, 1, &rawSrv);
        ctx->Draw(3, 0);
        ctx->PSSetShaderResources(0, 1, &nullSrv);
        ctx->OMSetRenderTargets(0, nullptr, nullptr);
    }

    // Яркость обычного белого на мониторе в режиме HDR (в единицах, где 1 = 80 нит).
    static float sdrWhite(const std::wstring& device) {
        UINT32 np = 0, nm = 0;
        if (GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, &np, &nm) != ERROR_SUCCESS || !np) return 3.0f;
        std::vector<DISPLAYCONFIG_PATH_INFO> paths(np);
        std::vector<DISPLAYCONFIG_MODE_INFO> modes(nm ? nm : 1);
        if (QueryDisplayConfig(QDC_ONLY_ACTIVE_PATHS, &np, paths.data(), &nm, modes.data(), nullptr) != ERROR_SUCCESS) return 3.0f;
        for (UINT32 i = 0; i < np; i++) {
            DISPLAYCONFIG_SOURCE_DEVICE_NAME sn{};
            sn.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
            sn.header.size = sizeof(sn);
            sn.header.adapterId = paths[i].sourceInfo.adapterId;
            sn.header.id = paths[i].sourceInfo.id;
            if (DisplayConfigGetDeviceInfo(&sn.header) != ERROR_SUCCESS || device != sn.viewGdiDeviceName) continue;
            DISPLAYCONFIG_SDR_WHITE_LEVEL wl{};
            wl.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL;
            wl.header.size = sizeof(wl);
            wl.header.adapterId = paths[i].targetInfo.adapterId;
            wl.header.id = paths[i].targetInfo.id;
            if (DisplayConfigGetDeviceInfo(&wl.header) == ERROR_SUCCESS && wl.SDRWhiteLevel >= 1000) return wl.SDRWhiteLevel / 1000.0f;
        }
        return 3.0f;
    }

    // Окно изменило размер: пересоздаём текстуры источника. Размер кадра на выходе не меняется —
    // картинка вписывается в него с полями.
    bool resizeSource(int w, int h) {
        SafeRelease(deskSrv); SafeRelease(stageSrv); SafeRelease(stageRtv); SafeRelease(deskRtv); SafeRelease(desk); SafeRelease(stage);
        const UINT both = D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET;
        bool ok = makeTex(w, h, deskFormat, both, false, &desk) && makeTex(w, h, deskFormat, both, false, &stage)
               && SUCCEEDED(dev->CreateShaderResourceView(desk, nullptr, &deskSrv))
               && SUCCEEDED(dev->CreateShaderResourceView(stage, nullptr, &stageSrv))
               && SUCCEEDED(dev->CreateRenderTargetView(stage, nullptr, &stageRtv))
               && SUCCEEDED(dev->CreateRenderTargetView(desk, nullptr, &deskRtv));
        srcW = w; srcH = h;
        return ok;
    }

    // Захват одного окна. Курсор и рамку рисует сама Windows.
    bool initWindow(HWND hwnd, int targetHeight, bool withCursor) {
        try {
            winrt::init_apartment(winrt::apartment_type::multi_threaded);
            if (!wgc::GraphicsCaptureSession::IsSupported()) { logf("error: window capture is not supported here"); return false; }
            static const D3D_FEATURE_LEVEL levels[] = { D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1, D3D_FEATURE_LEVEL_10_0 };
            D3D_FEATURE_LEVEL got;
            HRESULT hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                           levels, ARRAYSIZE(levels), D3D11_SDK_VERSION, &dev, &got, &ctx);
            if (FAILED(hr))
                hr = D3D11CreateDevice(nullptr, D3D_DRIVER_TYPE_WARP, nullptr, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                                       levels, ARRAYSIZE(levels), D3D11_SDK_VERSION, &dev, &got, &ctx);
            if (FAILED(hr)) { logf("error: D3D11CreateDevice 0x%08lx", hr); return false; }
            winrt::com_ptr<IDXGIDevice> dxgi;
            winrt::check_hresult(dev->QueryInterface(__uuidof(IDXGIDevice), dxgi.put_void()));
            winrt::com_ptr<::IInspectable> inspectable;
            winrt::check_hresult(CreateDirect3D11DeviceFromDXGIDevice(dxgi.get(), inspectable.put()));
            rtDevice = inspectable.as<wgd::Direct3D11::IDirect3DDevice>();
            auto interop = winrt::get_activation_factory<wgc::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
            winrt::check_hresult(interop->CreateForWindow(hwnd, winrt::guid_of<wgc::IGraphicsCaptureItem>(), winrt::put_abi(item)));
            const auto size = item.Size();
            width = srcW = std::max(2, (int)size.Width);
            height = srcH = std::max(2, (int)size.Height);
            pool = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(
                rtDevice, wgd::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, size);
            session = pool.CreateCaptureSession(item);
            try { session.IsBorderRequired(false); } catch (...) {}          // убрать жёлтую рамку умеет только Windows 11
            try { session.IsCursorCaptureEnabled(withCursor); } catch (...) {}
            item.Closed([this](auto&&, auto&&) { windowClosed = true; });
            session.StartCapture();
            windowMode = true;
            hwndTarget = hwnd;
            deskFormat = DXGI_FORMAT_B8G8R8A8_UNORM;
            return initGpu(targetHeight);
        } catch (winrt::hresult_error const& e) {
            logf("error: window capture 0x%08lx", (unsigned long)e.code().value);
            return false;
        }
    }

    // 1 — новая картинка окна, 0 — ничего нового, -2 — окно закрыто
    int pollWindow() {
        if (windowClosed || !IsWindow(hwndTarget)) return -2;   // событие «закрыто» приходит не всегда — проверяем и сами
        try {
            wgc::Direct3D11CaptureFrame frame{nullptr}, latest{nullptr};
            while ((frame = pool.TryGetNextFrame())) latest = frame;   // берём самый свежий кадр
            if (!latest) return 0;
            const auto content = latest.ContentSize();
            auto access = latest.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
            winrt::com_ptr<ID3D11Texture2D> tex;
            winrt::check_hresult(access->GetInterface(__uuidof(ID3D11Texture2D), tex.put_void()));
            D3D11_TEXTURE2D_DESC td;
            tex->GetDesc(&td);
            const int cw = std::max(2, std::min((int)content.Width, (int)td.Width));
            const int chh = std::max(2, std::min((int)content.Height, (int)td.Height));
            if (cw != srcW || chh != srcH) {
                if (!resizeSource(cw, chh)) return -2;
                logf("info: window resized to %dx%d", cw, chh);
            }
            D3D11_BOX box{};
            box.right = (UINT)cw; box.bottom = (UINT)chh; box.back = 1;
            ctx->CopySubresourceRegion(desk, 0, 0, 0, 0, tex.get(), 0, &box);
            if ((int)content.Width != (int)td.Width || (int)content.Height != (int)td.Height)
                pool.Recreate(rtDevice, wgd::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, content);
            haveImage = true;
            images++;
            return 1;
        } catch (winrt::hresult_error const& e) {
            logf("info: window capture error 0x%08lx", (unsigned long)e.code().value);
            return -2;
        }
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

    // Общая часть: настройка конвейера и курсор поверх картинки. Возвращает, откуда брать картинку.
    ID3D11ShaderResourceView* prepare(bool drawCursor) {
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
        return src;
    }

    // Куда в кадре ложится картинка. Окно вписывается целиком, по краям — чёрные поля (true — поля есть).
    bool placement(float& vx, float& vy, float& vw, float& vh) {
        vx = 0; vy = 0; vw = (float)outW; vh = (float)outH;
        if (srcW <= 0 || srcH <= 0) return false;
        if (!windowMode && srcW == width && srcH == height) return false;   // экран в исходном размере — во весь кадр
        const float k = std::min((float)outW / srcW, (float)outH / srcH);
        vw = srcW * k; vh = srcH * k;
        vx = (outW - vw) / 2; vy = (outH - vh) / 2;
        return vw < outW - 0.5f || vh < outH - 0.5f;
    }

    // Готовый кадр NV12: курсор, уменьшение и перевод цвета — на видеокарте, копируется только результат.
    bool composeGpu(std::vector<uint8_t>& dst, bool drawCursor) {
        ID3D11ShaderResourceView* nullSrv = nullptr;
        ID3D11ShaderResourceView* src = prepare(drawCursor);
        float vx, vy, vw, vh;
        if (placement(vx, vy, vw, vh)) {
            const float blackY[4] = { 0.0627f, 0, 0, 1 }, blackUV[4] = { 0.502f, 0.502f, 0, 1 };
            ctx->ClearRenderTargetView(yRtv, blackY);
            ctx->ClearRenderTargetView(uvRtv, blackUV);
        }
        ctx->OMSetRenderTargets(1, &yRtv, nullptr);      // яркость
        viewport(vx, vy, vw, vh);
        ctx->PSSetShader(psY, nullptr, 0);
        ctx->PSSetShaderResources(0, 1, &src);
        ctx->Draw(3, 0);
        ctx->OMSetRenderTargets(1, &uvRtv, nullptr);     // цвет, вдвое меньше
        viewport(vx / 2, vy / 2, vw / 2, vh / 2);
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

    // Готовый кадр прямо в текстуру кодировщика: картинка так и остаётся в памяти видеокарты.
    void composeTo(ID3D11RenderTargetView* target, bool drawCursor) {
        ID3D11ShaderResourceView* nullSrv = nullptr;
        ID3D11ShaderResourceView* src = prepare(drawCursor);
        float vx, vy, vw, vh;
        if (placement(vx, vy, vw, vh)) {
            const float black[4] = { 0, 0, 0, 1 };
            ctx->ClearRenderTargetView(target, black);
        }
        ctx->OMSetRenderTargets(1, &target, nullptr);
        viewport(vx, vy, vw, vh);
        ctx->PSSetShader(psOut, nullptr, 0);
        ctx->PSSetShaderResources(0, 1, &src);
        ctx->Draw(3, 0);
        ctx->PSSetShaderResources(0, 1, &nullSrv);
        ctx->OMSetRenderTargets(0, nullptr, nullptr);
        ctx->Flush();   // кодировщик работает с текстурой сам — рисование должно быть уже отправлено
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

    // 1 — готово, 0 — пока не вышло (повторим позже), -1 — размер экрана изменился (только без обработки
    // на видеокарте: с ней новый размер просто вписывается в прежний кадр, запись не прерывается)
    int createDupl() {
        HRESULT hr = E_FAIL;
        if (wantGpu) {   // монитор в режиме HDR — берём картинку как есть и сами приводим к обычным цветам
            bool hdrOn = false;
            IDXGIOutput6* o6 = nullptr;
            if (SUCCEEDED(out1->QueryInterface(__uuidof(IDXGIOutput6), (void**)&o6))) {
                DXGI_OUTPUT_DESC1 d1;
                if (SUCCEEDED(o6->GetDesc1(&d1))) {
                    hdrOn = d1.ColorSpace == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020;
                    deviceName = d1.DeviceName;
                }
                o6->Release();
            }
            IDXGIOutput5* o5 = nullptr;
            if (hdrOn && SUCCEEDED(out1->QueryInterface(__uuidof(IDXGIOutput5), (void**)&o5))) {
                const DXGI_FORMAT formats[] = { DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_B8G8R8A8_UNORM };
                hr = o5->DuplicateOutput1(dev, 0, ARRAYSIZE(formats), formats, &dupl);
                o5->Release();
                if (FAILED(hr)) { dupl = nullptr; logf("info: hdr capture unavailable 0x%08lx, using plain", hr); }
            }
        }
        if (FAILED(hr)) hr = out1->DuplicateOutput(dev, &dupl);
        if (FAILED(hr)) { lastError = hr; dupl = nullptr; return 0; }
        DXGI_OUTDUPL_DESC d;
        dupl->GetDesc(&d);
        const int w = (int)d.ModeDesc.Width, h = (int)d.ModeDesc.Height;
        const DXGI_FORMAT fmt = d.ModeDesc.Format == DXGI_FORMAT_UNKNOWN ? DXGI_FORMAT_B8G8R8A8_UNORM : d.ModeDesc.Format;
        int r = 0;
        if (wantGpu) r = d.Rotation == DXGI_MODE_ROTATION_ROTATE90 ? 1 : d.Rotation == DXGI_MODE_ROTATION_ROTATE180 ? 2
                       : d.Rotation == DXGI_MODE_ROTATION_ROTATE270 ? 3 : 0;
        const int uw = (r & 1) ? h : w, uh = (r & 1) ? w : h;   // размер «как на столе»
        const bool first = width == 0;
        if (first) {
            width = srcW = uw; height = srcH = uh;
            base.assign((size_t)w * h * 4, 0);
        } else if (!gpu && (w != width || h != height)) {
            logf("error: screen size changed %dx%d -> %dx%d", width, height, w, h);
            return -1;
        }
        if (wantGpu) {
            const bool isHdr = fmt == DXGI_FORMAT_R16G16B16A16_FLOAT;
            const bool changed = first || rot != r || hdr != isHdr || rawW != w || rawH != h || rawFormat != fmt;
            rot = r; hdr = isHdr; rawW = w; rawH = h; rawFormat = fmt;
            white = isHdr ? sdrWhite(deviceName) : 1.0f;
            prepass = rot != 0 || fmt != DXGI_FORMAT_B8G8R8A8_UNORM;
            if (changed) logf("info: source %dx%d rotation=%d hdr=%d white=%.0f", uw, uh, rot * 90, isHdr ? 1 : 0, white * 80);
            if (gpu && changed) {   // экран изменился на ходу: кадр на выходе остаётся прежним
                if ((uw != srcW || uh != srcH) && !resizeSource(uw, uh)) return -1;
                if (!makeRaw()) return -1;
                haveImage = false;
            }
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
                    if (prepass && raw) { ctx->CopyResource(raw, tex); runPrepass(); }
                    else ctx->CopyResource(desk, tex);
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
        try {
            if (session) session.Close();
            if (pool) pool.Close();
        } catch (...) {}
        session = nullptr; pool = nullptr; item = nullptr; rtDevice = nullptr;
        SafeRelease(curColorSrv); SafeRelease(curInvertSrv); SafeRelease(curColor); SafeRelease(curInvert);
        SafeRelease(deskSrv); SafeRelease(stageSrv); SafeRelease(stageRtv); SafeRelease(yRtv); SafeRelease(uvRtv);
        SafeRelease(desk); SafeRelease(stage); SafeRelease(yTex); SafeRelease(uvTex); SafeRelease(yRead); SafeRelease(uvRead);
        SafeRelease(vsFull); SafeRelease(psCopy); SafeRelease(psY); SafeRelease(psUV); SafeRelease(psOut);
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
struct QItem {
    FramePtr cpu;               // кадр в обычной памяти
    std::shared_ptr<void> hw;   // кадр в памяти видеокарты (AVFrame) — при кодировании в движке
    explicit operator bool() const { return cpu || hw; }
    void reset() { cpu.reset(); hw.reset(); }
};

struct FrameQueue {
    CRITICAL_SECTION cs;
    CONDITION_VARIABLE cv;
    std::deque<QItem> q;
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
    void push(const QItem& f) {
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
    bool pop(QItem& out) {
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
    QItem item;
    while (g_queue.pop(item)) {
        const double w0 = now();
        if (!writeAll(g_pipe, item.cpu->data(), item.cpu->size())) { InterlockedExchange(&g_writerFailed, 1); break; }
        const LONG us = (LONG)((now() - w0) * 1e6);
        if (us > g_writeMaxMicros) InterlockedExchange(&g_writeMaxMicros, us);
        if (us > g_writeMaxTotalMicros) InterlockedExchange(&g_writeMaxTotalMicros, us);
        InterlockedIncrement64(&g_written);
        item.reset();
    }
    return 0;
}

// ------------------------------------------------------------------ кодирование в движке
//
// Режим --encode: сжимаем видео прямо здесь. Кадр рисуется в текстуру, которую кодировщик
// видеокарты (NVENC, AMF) читает напрямую, — в обычную память попадает уже сжатое видео.
// В канал идёт H.264 в контейнере NUT с точным временем каждого кадра; ffmpeg только
// добавляет звук и раскладывает запись по кусочкам буфера, ничего не перекодируя.

static volatile LONG g_encFailed = 0;

#ifdef MC_FFMPEG
static void avLog(void*, int level, const char* fmt, va_list vl) {
    if (level > AV_LOG_WARNING) return;
    char buf[1024];
    vsnprintf(buf, sizeof(buf), fmt, vl);
    size_t n = strlen(buf);
    while (n && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) buf[--n] = 0;
    if (n) logf("info: av: %s", buf);
}

static std::string avErr(int code) {
    char b[128] = { 0 };
    av_strerror(code, b, sizeof(b));
    return b;
}

// BGRA -> NV12 на процессоре (BT.709, 16..235). Нужен только там, где кадр и так в обычной памяти.
static void bgraToNv12(const uint8_t* src, int stride, int w, int h, uint8_t* y, int ys, uint8_t* uv, int uvs) {
    for (int yy = 0; yy < h; yy += 2) {
        const uint8_t* rows[2] = { src + (size_t)yy * stride, src + (size_t)(yy + 1) * stride };
        uint8_t* yout[2] = { y + (size_t)yy * ys, y + (size_t)(yy + 1) * ys };
        uint8_t* c = uv + (size_t)(yy / 2) * uvs;
        for (int xx = 0; xx < w; xx += 2) {
            int sb = 0, sg = 0, sr = 0;
            for (int k = 0; k < 4; k++) {
                const uint8_t* px = rows[k >> 1] + (size_t)(xx + (k & 1)) * 4;
                const int b = px[0], g = px[1], r = px[2];
                sb += b; sg += g; sr += r;
                yout[k >> 1][xx + (k & 1)] = (uint8_t)(((16 << 16) + 32768 + 11967 * r + 40252 * g + 4063 * b) >> 16);
            }
            c[xx] = (uint8_t)(((128 << 18) + (1 << 17) - 6593 * sr - 22190 * sg + 28783 * sb) >> 18);
            c[xx + 1] = (uint8_t)(((128 << 18) + (1 << 17) + 28783 * sr - 26142 * sg - 2641 * sb) >> 18);
        }
    }
}

struct Encoder {
    enum { CPU = 0, GPU = 1, DOWNLOAD = 2 };
    int mode = CPU;
    bool srcNv12 = false;
    int w = 0, h = 0, fps = 60, srcStride = 0;
    AVCodecContext* enc = nullptr;
    AVFormatContext* oc = nullptr;
    AVStream* st = nullptr;
    AVIOContext* io = nullptr;
    AVBufferRef *hwDevice = nullptr, *hwFrames = nullptr;
    AVPacket* pkt = nullptr;
    AVFrame *nv = nullptr, *sw = nullptr, *planar = nullptr;   // planar — для кодировщиков, не принимающих NV12 (x265)
    const void* lastSrc = nullptr;
    long long pts = 0, bytes = 0, packets = 0;
    volatile LONG ioFailed = 0;
    ID3D11Device* dev = nullptr;
    std::map<ID3D11Texture2D*, ID3D11RenderTargetView*> views;

    // Режим «без ffmpeg»: звук кодируется здесь же (AAC), всё упаковывается в MPEG-TS.
    struct AudioEnc {
        AVCodecContext* ctx = nullptr;
        AVStream* st = nullptr;
        AVFrame* frame = nullptr;
        AVPacket* pkt = nullptr;
        std::vector<float> fifo;
        long long pts = 0;
    };
    bool mux = false;
    std::vector<AudioEnc> audio;   // 0 — общая дорожка, дальше отдельные
    long long audioShift = 0;      // сдвиг звука, в отсчётах
    std::mutex muxLock;
    volatile LONG muxReady = 0;

    bool initAudio(int count, int offsetMs) {
        audioShift = (long long)offsetMs * 48;
        const AVCodec* codec = avcodec_find_encoder(AV_CODEC_ID_AAC);
        if (!codec) { logf("error: encoder: no aac"); return false; }
        audio.resize((size_t)count);
        for (AudioEnc& a : audio) {
            a.ctx = avcodec_alloc_context3(codec);
            if (!a.ctx) return false;
            a.ctx->sample_fmt = AV_SAMPLE_FMT_FLTP;
            a.ctx->sample_rate = 48000;
            a.ctx->bit_rate = 160000;
            a.ctx->time_base = AVRational{ 1, 48000 };
            av_channel_layout_default(&a.ctx->ch_layout, 2);
            const int ret = avcodec_open2(a.ctx, codec, nullptr);
            if (ret < 0) { logf("error: encoder: aac: %s", avErr(ret).c_str()); return false; }
            a.frame = av_frame_alloc();
            a.pkt = av_packet_alloc();
            if (!a.frame || !a.pkt) return false;
            a.frame->format = AV_SAMPLE_FMT_FLTP;
            a.frame->nb_samples = a.ctx->frame_size > 0 ? a.ctx->frame_size : 1024;
            a.frame->sample_rate = 48000;
            av_channel_layout_copy(&a.frame->ch_layout, &a.ctx->ch_layout);
            if (av_frame_get_buffer(a.frame, 0) < 0) return false;
        }
        return true;
    }

    // Пакет — в общий поток. Видео и звук пишут два разных потока программы.
    bool writePacket(AVPacket* p) {
        std::lock_guard<std::mutex> lock(muxLock);
        if (!muxReady) { av_packet_unref(p); return true; }
        const int ret = mux ? av_interleaved_write_frame(oc, p) : av_write_frame(oc, p);
        av_packet_unref(p);
        if (ret < 0) { logf("error: encoder: write: %s", avErr(ret).c_str()); return false; }
        avio_flush(io);
        return true;
    }

    // Очередная порция сведённого звука одной дорожки (стерео, вперемежку).
    bool feedAudio(size_t track, const float* samples, long long frames) {
        if (track >= audio.size() || !muxReady) return true;
        AudioEnc& a = audio[track];
        a.fifo.insert(a.fifo.end(), samples, samples + frames * 2);
        const int n = a.frame->nb_samples;
        size_t used = 0;
        while (a.fifo.size() - used >= (size_t)n * 2) {
            if (av_frame_make_writable(a.frame) < 0) return false;
            float* left = (float*)a.frame->data[0];
            float* right = (float*)a.frame->data[1];
            for (int i = 0; i < n; i++) {
                const float l = a.fifo[used + (size_t)i * 2], r = a.fifo[used + (size_t)i * 2 + 1];
                left[i] = l > 1.0f ? 1.0f : (l < -1.0f ? -1.0f : l);
                right[i] = r > 1.0f ? 1.0f : (r < -1.0f ? -1.0f : r);
            }
            used += (size_t)n * 2;
            a.frame->pts = a.pts + audioShift;
            a.pts += n;
            int ret = avcodec_send_frame(a.ctx, a.frame);
            if (ret < 0) { logf("error: encoder: aac send: %s", avErr(ret).c_str()); return false; }
            for (;;) {
                ret = avcodec_receive_packet(a.ctx, a.pkt);
                if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) break;
                if (ret < 0) { logf("error: encoder: aac receive: %s", avErr(ret).c_str()); return false; }
                a.pkt->stream_index = a.st->index;
                av_packet_rescale_ts(a.pkt, a.ctx->time_base, a.st->time_base);
                if (!writePacket(a.pkt)) return false;
            }
        }
        a.fifo.erase(a.fifo.begin(), a.fifo.begin() + (ptrdiff_t)used);
        return true;
    }

    static int writeCb(void* opaque, const uint8_t* buf, int size) {
        Encoder* e = (Encoder*)opaque;
        if (!e->ioFailed && !writeAll(g_pipe, buf, (size_t)size)) InterlockedExchange(&e->ioFailed, 1);   // читатель закрылся
        e->bytes += size;
        return size;
    }

    // Кодировщик будет брать кадры из нашего же устройства Direct3D — того, которым снимается экран.
    bool initHw(ID3D11Device* device, ID3D11DeviceContext* context) {
        dev = device;
        hwDevice = av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);
        if (!hwDevice) return false;
        AVHWDeviceContext* dc = (AVHWDeviceContext*)hwDevice->data;
        AVD3D11VADeviceContext* d3d = (AVD3D11VADeviceContext*)dc->hwctx;
        device->AddRef();
        d3d->device = device;
        context->AddRef();
        d3d->device_context = context;
        // FFmpeg требует ещё и видеоинтерфейсы устройства, хотя кодировщику они не нужны. У программной
        // видеокарты (сервер сборки) их нет — подставляем само устройство: с ним только вызовут Release.
        if (FAILED(device->QueryInterface(__uuidof(ID3D11VideoDevice), (void**)&d3d->video_device))) {
            device->AddRef();
            d3d->video_device = (ID3D11VideoDevice*)device;
            logf("info: encoder: no video device interface here");
        }
        if (FAILED(context->QueryInterface(__uuidof(ID3D11VideoContext), (void**)&d3d->video_context))) {
            context->AddRef();
            d3d->video_context = (ID3D11VideoContext*)context;
        }
        ID3D10Multithread* mt = nullptr;   // устройством теперь пользуются два потока
        if (SUCCEEDED(device->QueryInterface(__uuidof(ID3D10Multithread), (void**)&mt))) {
            mt->SetMultithreadProtected(TRUE);
            mt->Release();
        }
        int ret = av_hwdevice_ctx_init(hwDevice);
        if (ret < 0) { logf("error: encoder: device: %s", avErr(ret).c_str()); return false; }
        hwFrames = av_hwframe_ctx_alloc(hwDevice);
        if (!hwFrames) return false;
        AVHWFramesContext* fc = (AVHWFramesContext*)hwFrames->data;
        fc->format = AV_PIX_FMT_D3D11;
        fc->sw_format = AV_PIX_FMT_BGRA;
        fc->width = w;
        fc->height = h;
        fc->initial_pool_size = 0;
        AVD3D11VAFramesContext* f3 = (AVD3D11VAFramesContext*)fc->hwctx;
        f3->BindFlags = D3D11_BIND_RENDER_TARGET | D3D11_BIND_SHADER_RESOURCE;
        ret = av_hwframe_ctx_init(hwFrames);
        if (ret < 0) { logf("error: encoder: frames: %s", avErr(ret).c_str()); return false; }
        return true;
    }

    bool init(const std::string& name, int width, int height, int rate, int mbps, int wantMode, bool nv12Source,
              int sourceStride, ID3D11Device* device, ID3D11DeviceContext* context) {
        av_log_set_callback(avLog);
        const AVCodec* codec = avcodec_find_encoder_by_name(name.c_str());
        if (!codec) { logf("error: encoder %s not found", name.c_str()); return false; }
        mode = wantMode;
        w = width & ~1; h = height & ~1; fps = rate;
        srcNv12 = nv12Source; srcStride = sourceStride;
        enc = avcodec_alloc_context3(codec);
        if (!enc) return false;
        enc->width = w;
        enc->height = h;
        enc->time_base = AVRational{ 1, fps };
        enc->framerate = AVRational{ fps, 1 };
        enc->bit_rate = (int64_t)mbps * 1000000;
        enc->rc_max_rate = (int64_t)mbps * 1500000;
        enc->rc_buffer_size = mbps * 2000000;
        enc->gop_size = fps;       // опорный кадр раз в секунду: по ним режутся кусочки буфера
        enc->max_b_frames = 0;
        if (mode != CPU && !initHw(device, context)) return false;
        if (mode == GPU) {
            enc->pix_fmt = AV_PIX_FMT_D3D11;
            enc->hw_frames_ctx = av_buffer_ref(hwFrames);
        } else {
            enc->pix_fmt = AV_PIX_FMT_NV12;
            enc->colorspace = AVCOL_SPC_BT709;
            enc->color_primaries = AVCOL_PRI_BT709;
            enc->color_trc = AVCOL_TRC_BT709;
            enc->color_range = AVCOL_RANGE_MPEG;
            nv = av_frame_alloc();
            sw = av_frame_alloc();
            if (!nv || !sw) return false;
            nv->format = AV_PIX_FMT_NV12;
            nv->width = w;
            nv->height = h;
            if (av_frame_get_buffer(nv, 0) < 0) return false;
            if (name == "libx265") {
                enc->pix_fmt = AV_PIX_FMT_YUV420P;
                planar = av_frame_alloc();
                if (!planar) return false;
                planar->format = AV_PIX_FMT_YUV420P;
                planar->width = w;
                planar->height = h;
                if (av_frame_get_buffer(planar, 0) < 0) return false;
            }
        }
        AVDictionary* opts = nullptr;
        const auto has = [&](const char* part) { return name.find(part) != std::string::npos; };
        if (has("_nvenc")) {
            av_dict_set(&opts, "preset", fps <= 60 ? "p4" : "p2", 0);   // при высокой частоте — пресет побыстрее
            av_dict_set(&opts, "rc", "vbr", 0);
        } else if (has("_amf")) {
            av_dict_set(&opts, "usage", "transcoding", 0);
            av_dict_set(&opts, "quality", "balanced", 0);
            av_dict_set(&opts, "rc", "vbr_peak", 0);
            if (name == "hevc_amf") av_dict_set(&opts, "header_insertion_mode", "idr", 0);
        } else if (has("_qsv")) {
            av_dict_set(&opts, "preset", "veryfast", 0);
        } else if (name == "libx265") {
            char params[160];
            snprintf(params, sizeof(params), "keyint=%d:min-keyint=%d:scenecut=0:repeat-headers=1:log-level=error", fps, fps);
            av_dict_set(&opts, "preset", "ultrafast", 0);
            av_dict_set(&opts, "tune", "zerolatency", 0);
            av_dict_set(&opts, "x265-params", params, 0);
        } else {
            av_dict_set(&opts, "preset", "ultrafast", 0);
            av_dict_set(&opts, "tune", "zerolatency", 0);
            av_dict_set(&opts, "sc_threshold", "0", 0);
            enc->keyint_min = fps;
        }
        const int ret = avcodec_open2(enc, codec, &opts);
        av_dict_free(&opts);
        if (ret < 0) { logf("error: encoder %s: %s", name.c_str(), avErr(ret).c_str()); return false; }
        pkt = av_packet_alloc();
        logf("info: encoder %s %dx%d %d fps %d Mbit/s frames=%s", name.c_str(), w, h, fps, mbps,
             mode == GPU ? "gpu" : (mode == DOWNLOAD ? "download" : "cpu"));
        return pkt != nullptr;
    }

    // Начать поток в канал (канал к этому моменту уже открыт читателем).
    bool open() {
        int ret = avformat_alloc_output_context2(&oc, nullptr, mux ? "mpegts" : "nut", nullptr);
        if (ret < 0 || !oc) { logf("error: encoder: muxer: %s", avErr(ret).c_str()); return false; }
        unsigned char* buf = (unsigned char*)av_malloc(1 << 16);
        io = avio_alloc_context(buf, 1 << 16, 1, this, nullptr, writeCb, nullptr);
        if (!io) return false;
        oc->pb = io;
        oc->flags |= AVFMT_FLAG_CUSTOM_IO | AVFMT_FLAG_FLUSH_PACKETS;
        st = avformat_new_stream(oc, nullptr);
        if (!st) return false;
        avcodec_parameters_from_context(st->codecpar, enc);
        st->time_base = enc->time_base;
        st->avg_frame_rate = st->r_frame_rate = enc->framerate;
        for (AudioEnc& a : audio) {
            a.st = avformat_new_stream(oc, nullptr);
            if (!a.st) return false;
            avcodec_parameters_from_context(a.st->codecpar, a.ctx);
            a.st->time_base = a.ctx->time_base;
        }
        ret = avformat_write_header(oc, nullptr);
        if (ret < 0) { logf("error: encoder: header: %s", avErr(ret).c_str()); return false; }
        avio_flush(io);
        InterlockedExchange(&muxReady, 1);
        return true;
    }

    // Свободная текстура под новый кадр (текстуры переиспользуются) и «холст» для рисования в неё.
    std::shared_ptr<void> newFrame(ID3D11RenderTargetView** view) {
        AVFrame* f = av_frame_alloc();
        if (!f) return nullptr;
        const int ret = av_hwframe_get_buffer(hwFrames, f, 0);
        if (ret < 0) { logf("error: encoder: frame: %s", avErr(ret).c_str()); av_frame_free(&f); return nullptr; }
        ID3D11Texture2D* tex = (ID3D11Texture2D*)f->data[0];
        auto it = views.find(tex);
        if (it == views.end()) {
            ID3D11RenderTargetView* v = nullptr;
            const HRESULT hr = dev->CreateRenderTargetView(tex, nullptr, &v);
            if (FAILED(hr)) { logf("error: encoder: render target 0x%08lx", hr); av_frame_free(&f); return nullptr; }
            it = views.emplace(tex, v).first;
        }
        *view = it->second;
        return std::shared_ptr<void>(f, [](void* p) { AVFrame* fr = (AVFrame*)p; av_frame_free(&fr); });
    }

    bool drain() {
        for (;;) {
            int ret = avcodec_receive_packet(enc, pkt);
            if (ret == AVERROR(EAGAIN) || ret == AVERROR_EOF) return true;
            if (ret < 0) { logf("error: encoder: receive: %s", avErr(ret).c_str()); return false; }
            pkt->stream_index = 0;
            if (pkt->duration <= 0) pkt->duration = 1;
            av_packet_rescale_ts(pkt, enc->time_base, st->time_base);
            if (!writePacket(pkt)) return false;
            packets++;
        }
    }

    bool encode(const QItem& item) {
        AVFrame* f = nullptr;
        if (mode == GPU) {
            f = (AVFrame*)item.hw.get();
        } else {
            const void* key = mode == DOWNLOAD ? (const void*)item.hw.get() : (const void*)item.cpu.get();
            if (key != lastSrc) {   // повтор прошлого кадра заново не готовим
                if (av_frame_make_writable(nv) < 0) return false;
                if (mode == DOWNLOAD) {
                    av_frame_unref(sw);
                    const int ret = av_hwframe_transfer_data(sw, (AVFrame*)item.hw.get(), 0);
                    if (ret < 0) { logf("error: encoder: download: %s", avErr(ret).c_str()); return false; }
                    if (sw->format != AV_PIX_FMT_BGRA) { logf("error: encoder: download format %d", sw->format); return false; }
                    bgraToNv12(sw->data[0], sw->linesize[0], w, h, nv->data[0], nv->linesize[0], nv->data[1], nv->linesize[1]);
                } else if (srcNv12) {
                    const uint8_t* src = item.cpu->data();
                    for (int yy = 0; yy < h; yy++) memcpy(nv->data[0] + (size_t)yy * nv->linesize[0], src + (size_t)yy * w, (size_t)w);
                    src += (size_t)w * h;
                    for (int yy = 0; yy < h / 2; yy++) memcpy(nv->data[1] + (size_t)yy * nv->linesize[1], src + (size_t)yy * w, (size_t)w);
                } else {
                    bgraToNv12(item.cpu->data(), srcStride, w, h, nv->data[0], nv->linesize[0], nv->data[1], nv->linesize[1]);
                }
                if (planar) {   // NV12 -> три отдельные плоскости
                    if (av_frame_make_writable(planar) < 0) return false;
                    for (int yy = 0; yy < h; yy++)
                        memcpy(planar->data[0] + (size_t)yy * planar->linesize[0], nv->data[0] + (size_t)yy * nv->linesize[0], (size_t)w);
                    for (int yy = 0; yy < h / 2; yy++) {
                        const uint8_t* uv = nv->data[1] + (size_t)yy * nv->linesize[1];
                        uint8_t* u = planar->data[1] + (size_t)yy * planar->linesize[1];
                        uint8_t* v = planar->data[2] + (size_t)yy * planar->linesize[2];
                        for (int xx = 0; xx < w / 2; xx++) { u[xx] = uv[xx * 2]; v[xx * 2 / 2] = uv[xx * 2 + 1]; }
                    }
                }
                lastSrc = key;
            }
            f = planar ? planar : nv;
        }
        if (!f) return false;
        f->pts = pts++;   // время кадра — по его номеру: кадры идут ровно с заданной частотой
        const int ret = avcodec_send_frame(enc, f);
        if (ret < 0) { logf("error: encoder: send: %s", avErr(ret).c_str()); return false; }
        return drain();
    }

    void finish() {
        if (!enc || !oc) return;
        if (avcodec_send_frame(enc, nullptr) >= 0) drain();
        std::lock_guard<std::mutex> lock(muxLock);
        InterlockedExchange(&muxReady, 0);   // звук после этого уже не пишется
        av_write_trailer(oc);
        avio_flush(io);
    }
};

static Encoder g_enc;

static DWORD WINAPI encodeThread(LPVOID) {
    QItem item;
    while (g_queue.pop(item)) {
        const double w0 = now();
        if (!g_enc.encode(item)) { InterlockedExchange(&g_encFailed, 1); break; }
        if (g_enc.ioFailed) { InterlockedExchange(&g_writerFailed, 1); break; }
        const LONG us = (LONG)((now() - w0) * 1e6);
        if (us > g_writeMaxMicros) InterlockedExchange(&g_writeMaxMicros, us);
        if (us > g_writeMaxTotalMicros) InterlockedExchange(&g_writeMaxTotalMicros, us);
        InterlockedIncrement64(&g_written);
        item.reset();
    }
    item.reset();
    if (!g_encFailed && !g_enc.ioFailed) g_enc.finish();
    return 0;
}
#endif

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

static std::string narrow(const std::wstring& w) {
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, 0);
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &out[0], n, nullptr, nullptr);
    return out;
}

struct ProcInfo { DWORD parent = 0; std::wstring exe; };

static std::map<DWORD, ProcInfo> snapshotProcesses() {
    std::map<DWORD, ProcInfo> out;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return out;
    PROCESSENTRY32W e{};
    e.dwSize = sizeof(e);
    if (Process32FirstW(snap, &e)) {
        do {
            ProcInfo p;
            p.parent = e.th32ParentProcessID;
            p.exe = e.szExeFile;
            for (auto& c : p.exe) c = (wchar_t)towlower(c);
            out[e.th32ProcessID] = p;
        } while (Process32NextW(snap, &e));
    }
    CloseHandle(snap);
    return out;
}

// Номера процессов, у которых есть звуковой сеанс на любом устройстве вывода.
static std::set<DWORD> sessionPids() {
    std::set<DWORD> out;
    IMMDeviceEnumerator* en = nullptr;
    if (FAILED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator), (void**)&en))) return out;
    IMMDeviceCollection* all = nullptr;
    if (SUCCEEDED(en->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &all))) {
        UINT count = 0;
        all->GetCount(&count);
        for (UINT i = 0; i < count; i++) {
            IMMDevice* d = nullptr;
            IAudioSessionManager2* mgr = nullptr;
            IAudioSessionEnumerator* sessions = nullptr;
            if (FAILED(all->Item(i, &d))) continue;
            if (SUCCEEDED(d->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr, (void**)&mgr))
                && SUCCEEDED(mgr->GetSessionEnumerator(&sessions))) {
                int n = 0;
                sessions->GetCount(&n);
                for (int j = 0; j < n; j++) {
                    IAudioSessionControl* c = nullptr;
                    IAudioSessionControl2* c2 = nullptr;
                    if (FAILED(sessions->GetSession(j, &c))) continue;
                    if (SUCCEEDED(c->QueryInterface(__uuidof(IAudioSessionControl2), (void**)&c2))) {
                        DWORD pid = 0;
                        AudioSessionState state = AudioSessionStateExpired;
                        c->GetState(&state);
                        if (c2->IsSystemSoundsSession() != S_OK && state != AudioSessionStateExpired
                            && SUCCEEDED(c2->GetProcessId(&pid)) && pid != 0) out.insert(pid);
                        c2->Release();
                    }
                    c->Release();
                }
            }
            SafeRelease(sessions); SafeRelease(mgr); SafeRelease(d);
        }
        all->Release();
    }
    en->Release();
    return out;
}

// «Корневые» процессы со звуком: у программы из нескольких процессов (браузер, Discord) берётся
// самый верхний с тем же именем — захват идёт сразу по всему его дереву процессов.
static std::set<DWORD> audioRoots(const std::map<DWORD, ProcInfo>& procs, DWORD skipA, DWORD skipB) {
    std::set<DWORD> roots;
    for (DWORD pid : sessionPids()) {
        auto it = procs.find(pid);
        if (it == procs.end() || pid == skipA || pid == skipB) continue;
        DWORD r = pid;
        for (int depth = 0; depth < 32; depth++) {
            auto cur = procs.find(r);
            if (cur == procs.end()) break;
            auto par = procs.find(cur->second.parent);
            if (par == procs.end() || par->first == r || par->second.exe != it->second.exe) break;
            r = par->first;
        }
        if (r != skipA && r != skipB) roots.insert(r);
    }
    return roots;
}

class ActivateHandler : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
    LONG ref_ = 1;
public:
    HANDLE done = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    virtual ~ActivateHandler() { CloseHandle(done); }
    STDMETHODIMP QueryInterface(REFIID riid, void** out) override {
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IActivateAudioInterfaceCompletionHandler))
            *out = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
        else if (riid == __uuidof(IAgileObject)) *out = static_cast<IAgileObject*>(this);
        else { *out = nullptr; return E_NOINTERFACE; }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return (ULONG)InterlockedIncrement(&ref_); }
    STDMETHODIMP_(ULONG) Release() override {
        const LONG n = InterlockedDecrement(&ref_);
        if (n == 0) delete this;
        return (ULONG)n;
    }
    STDMETHODIMP ActivateCompleted(IActivateAudioInterfaceAsyncOperation*) override { SetEvent(done); return S_OK; }
};

struct AudioSource {
    DWORD pid = 0;             // не ноль — это звук одной программы (и всех её дочерних процессов)
    std::wstring exe;
    HANDLE evt = nullptr;
    double discardUntil = 0;
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
    int failures = 0;

    void close() {
        SafeRelease(cap); SafeRelease(client); SafeRelease(dev);
        if (evt) { CloseHandle(evt); evt = nullptr; }
    }

    std::string label() const { return pid ? narrow(exe) : (loopback ? "system" : "mic"); }

    // Общая часть: попросить у Windows сразу 48 кГц, стерео (пересчёт она делает сама) и запустить.
    bool startClient(DWORD extraFlags, REFERENCE_TIME buffer) {
        WAVEFORMATEXTENSIBLE wf{};
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
        const DWORD flags = 0x80000000 /*AUTOCONVERTPCM*/ | 0x08000000 /*SRC_DEFAULT_QUALITY*/ | extraFlags;
        HRESULT hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, flags, buffer, 0, (WAVEFORMATEX*)&wf, nullptr);
        if (SUCCEEDED(hr) && (extraFlags & AUDCLNT_STREAMFLAGS_EVENTCALLBACK)) {
            evt = CreateEventW(nullptr, FALSE, FALSE, nullptr);
            hr = client->SetEventHandle(evt);
        }
        if (SUCCEEDED(hr)) hr = client->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
        if (SUCCEEDED(hr)) hr = client->Start();
        if (FAILED(hr)) { openError = hr; close(); return false; }
        return true;
    }

    // Звук одной программы: «петля по процессу» (Windows 10 2004 и новее).
    bool openProcess() {
        AUDIOCLIENT_ACTIVATION_PARAMS params{};
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
        params.ProcessLoopbackParams.TargetProcessId = pid;
        params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
        PROPVARIANT pv{};
        pv.vt = VT_BLOB;
        pv.blob.cbSize = sizeof(params);
        pv.blob.pBlobData = (BYTE*)&params;
        ActivateHandler* handler = new ActivateHandler();
        IActivateAudioInterfaceAsyncOperation* op = nullptr;
        HRESULT hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &pv, handler, &op);
        if (SUCCEEDED(hr)) {
            hr = E_FAIL;
            if (WaitForSingleObject(handler->done, 3000) == WAIT_OBJECT_0) {
                HRESULT result = E_FAIL;
                IUnknown* unk = nullptr;
                if (SUCCEEDED(op->GetActivateResult(&result, &unk)) && SUCCEEDED(result) && unk)
                    hr = unk->QueryInterface(__uuidof(IAudioClient), (void**)&client);
                else hr = FAILED(result) ? result : E_FAIL;
                SafeRelease(unk);
            }
        }
        SafeRelease(op);
        handler->Release();
        if (FAILED(hr) || !client) { openError = hr; client = nullptr; return false; }
        return startClient(AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK, 0);
    }

    bool open() {
        if (pid) return openProcess();
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
        if (!startClient(loopback ? AUDCLNT_STREAMFLAGS_LOOPBACK : 0, 2000000 /*200 мс*/)) return false;
        // Некоторые микрофоны (виртуальные кабели) при старте отдают остатки старого звука — первую
        // четверть секунды выбрасываем.
        if (!loopback) discardUntil = now() + 0.25;
        return true;
    }

    // Забрать всё, что пришло с устройства, в очередь.
    void poll() {
        if (!cap) {
            if (now() < nextOpen) return;
            nextOpen = now() + 1.0;
            openError = S_OK;
            if (!open()) {
                failures++;
                if (!reported) {   // 0x80070005 — Windows запретила доступ (настройки конфиденциальности)
                    logf("info: audio %s unavailable 0x%08lx", label().c_str(), openError);
                    reported = true;
                }
                return;
            }
            reopened++;
            logf("info: audio %s opened", label().c_str());
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
            logf("info: audio %s lost 0x%08lx", label().c_str(), hr);
            close();
            nextOpen = now() + 0.5;
        }
    }

    void push(const float* data, UINT32 frames, bool silent) {
        if (g_t0 == 0 || frames == 0 || now() < discardUntil) return;
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
    int track = 0;   // номер отдельной дорожки (0 — только общая)
    void mixInto(std::vector<float>& out, std::vector<float>* extra, long long pos, long long count) {
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
        for (long long i = 0; i < n * 2; i++) {
            const float v = fifo[(size_t)i] * gain;
            out[(size_t)(offset * 2 + i)] += v;
            if (extra) (*extra)[(size_t)(offset * 2 + i)] += v;
        }
        fifo.erase(fifo.begin(), fifo.begin() + (size_t)n * 2);
        frontPos += n;
    }
};

static HANDLE g_audioPipe = INVALID_HANDLE_VALUE;
static std::vector<AudioSource*> g_sources;
static volatile LONG g_audioStop = 0, g_audioFailed = 0;
static volatile LONG64 g_audioWritten = 0;

static std::mutex g_audioInfoLock;
static std::string g_audioInfo;   // состояние источников звука — для статистики
static std::map<std::wstring, float> g_appVol;   // своя громкость программ; пусто — обычный режим (общий звук)
static float g_volSystem = 1.0f;
static DWORD g_parentPid = 0;
static bool g_perApp = false;

static bool g_muxAudio = false;
static std::map<std::wstring, int> g_appTrack;
static int g_tracks = 0, g_trackOther = 0;
static bool g_includeParent = false;

static float gainOf(const std::wstring& exe) {
    auto it = g_appVol.find(exe);
    return it == g_appVol.end() ? g_volSystem : it->second;
}
static int trackOf(const std::wstring& exe) {
    auto it = g_appTrack.find(exe);
    return it == g_appTrack.end() ? g_trackOther : it->second;
}

// Канал отдельной дорожки. ffmpeg открывает каналы по очереди, поэтому пока дорожку не открыли,
// её звук копится в памяти — тогда все дорожки начинаются с одного и того же момента.
struct TrackPipe {
    HANDLE h = INVALID_HANDLE_VALUE;
    volatile LONG connected = 0;
    std::vector<uint8_t> pending;
};
static std::vector<TrackPipe*> g_trackPipes;
static DWORD WINAPI trackConnect(LPVOID p) {
    TrackPipe* t = (TrackPipe*)p;
    if (ConnectNamedPipe(t->h, nullptr) || GetLastError() == ERROR_PIPE_CONNECTED) InterlockedExchange(&t->connected, 1);
    return 0;
}
static bool writeTrack(TrackPipe* t, const uint8_t* data, size_t n) {
    if (!t->connected) {
        if (t->pending.size() < (size_t)kRate * 4 * 120) t->pending.insert(t->pending.end(), data, data + n);
        return true;
    }
    if (!t->pending.empty()) {
        if (!writeAll(t->h, t->pending.data(), t->pending.size())) return false;
        std::vector<uint8_t>().swap(t->pending);
    }
    return writeAll(t->h, data, n);
}

// Режим «по программам»: держим по одному источнику на каждую программу со звуком.
static void refreshApps() {
    const std::map<DWORD, ProcInfo> procs = snapshotProcesses();
    std::set<DWORD> roots = audioRoots(procs, GetCurrentProcessId(), g_includeParent ? 0 : g_parentPid);
    // Захват идёт по дереву процессов. Если одна программа со звуком запущена из другой (игра из
    // лаунчера), их нельзя снять обе, не записав игру дважды: при одинаковой громкости берём
    // верхнюю (она включает нижнюю), при разной — нижнюю.
    std::set<DWORD> drop;
    for (DWORD r : roots) {
        DWORD cur = r;
        for (int depth = 0; depth < 32; depth++) {
            auto it = procs.find(cur);
            if (it == procs.end() || it->second.parent == cur) break;
            cur = it->second.parent;
            if (roots.count(cur)) {
                const float g1 = gainOf(procs.at(cur).exe), g2 = gainOf(procs.at(r).exe);
                drop.insert(g1 == g2 ? r : cur);
            }
        }
    }
    for (DWORD d : drop) roots.erase(d);
    for (auto it = roots.begin(); it != roots.end();) {   // программа выключена в записи — её звук и не снимаем
        if (gainOf(procs.at(*it).exe) <= 0.0f) it = roots.erase(it); else ++it;
    }
    for (size_t i = 0; i < g_sources.size();) {   // программа закрылась или больше не нужна
        AudioSource* s = g_sources[i];
        if (s->pid && !roots.count(s->pid)) {
            logf("info: audio %s closed", s->label().c_str());
            s->close();
            delete s;
            g_sources.erase(g_sources.begin() + i);
        } else i++;
    }
    for (DWORD r : roots) {
        bool have = false;
        for (AudioSource* s : g_sources) have = have || s->pid == r;
        if (have) continue;
        AudioSource* s = new AudioSource();
        s->pid = r;
        s->exe = procs.at(r).exe;
        s->gain = gainOf(s->exe);
        s->track = trackOf(s->exe);
        g_sources.push_back(s);
    }
    // Если «петля по процессу» здесь не работает совсем (старая Windows) — возвращаемся к общему звуку.
    int failed = 0, opened = 0;
    for (AudioSource* s : g_sources) if (s->pid) { if (s->cap) opened++; else if (s->failures >= 3) failed++; }
    if (failed && !opened) {
        logf("info: audio per-app unavailable, using the common mix");
        for (size_t i = 0; i < g_sources.size();) {
            if (g_sources[i]->pid) { g_sources[i]->close(); delete g_sources[i]; g_sources.erase(g_sources.begin() + i); }
            else i++;
        }
        AudioSource* s = new AudioSource();
        s->loopback = true;
        s->gain = g_volSystem;
        s->track = g_trackOther;
        g_sources.push_back(s);
        g_perApp = false;
    }
}

static DWORD WINAPI audioThread(LPVOID) {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_TIME_CRITICAL);
    for (TrackPipe* t : g_trackPipes) CloseHandle(CreateThread(nullptr, 0, trackConnect, t, 0, nullptr));
    std::vector<std::vector<float>> trackMix((size_t)(g_muxAudio ? g_tracks : (int)g_trackPipes.size()));
    if (g_muxAudio) {   // звук кодируется и упаковывается здесь же — ждём, пока откроется общий поток
#ifdef MC_FFMPEG
        while (!g_enc.muxReady && !g_audioStop) Sleep(2);
#endif
    } else if (!ConnectNamedPipe(g_audioPipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
        logf("error: audio pipe %lu", GetLastError());
        InterlockedExchange(&g_audioFailed, 1);
        return 0;
    }
    long long outPos = 0;
    std::vector<float> mix;
    std::vector<int16_t> pcm;
    double nextRefresh = 0, nextInfo = 0;
    while (!g_audioStop) {
        if (g_perApp && now() >= nextRefresh) {
            refreshApps();
            nextRefresh = now() + 0.5;
        }
        for (AudioSource* s : g_sources) s->poll();
        if (now() >= nextInfo) {
            std::string info;
            for (AudioSource* s : g_sources) {
                char part[160];
                snprintf(part, sizeof(part), " %s=%s/%lld", s->label().c_str(), s->cap ? "on" : "off", s->captured);
                info += part;
            }
            std::lock_guard<std::mutex> lock(g_audioInfoLock);
            g_audioInfo = info;
            nextInfo = now() + 1.0;
        }
        if (g_t0 != 0) {
            outPos += g_audioSkip.exchange(0);   // из видео выбросили кадры — выбрасываем столько же звука
            const long long target = (long long)((now() - g_t0 - kAudioDelay) * kRate);
            const long long due = std::min<long long>(target - outPos, kRate);
            if (due > 0) {
                mix.assign((size_t)due * 2, 0.0f);
                for (auto& t : trackMix) t.assign((size_t)due * 2, 0.0f);
                for (AudioSource* s : g_sources)
                    s->mixInto(mix, s->track > 0 && s->track <= (int)trackMix.size() ? &trackMix[(size_t)s->track - 1] : nullptr, outPos, due);
#ifdef MC_FFMPEG
                if (g_muxAudio) {
                    bool ok = g_enc.feedAudio(0, mix.data(), due);
                    for (size_t k = 0; k < trackMix.size() && ok; k++) ok = g_enc.feedAudio(k + 1, trackMix[k].data(), due);
                    if (!ok) { InterlockedExchange(&g_encFailed, 1); break; }
                    outPos += due;
                    InterlockedAdd64(&g_audioWritten, due);
                    Sleep(5);
                    continue;
                }
#endif
                pcm.resize((size_t)due * 2);
                bool tracksOk = true;
                for (size_t k = 0; k < trackMix.size() && tracksOk; k++) {   // отдельные дорожки
                    for (size_t i = 0; i < pcm.size(); i++) {
                        float v = trackMix[k][i] * 32767.0f;
                        pcm[i] = (int16_t)(v > 32767.0f ? 32767.0f : (v < -32768.0f ? -32768.0f : v));
                    }
                    tracksOk = writeTrack(g_trackPipes[k], (const uint8_t*)pcm.data(), pcm.size() * 2);
                }
                if (!tracksOk) { InterlockedExchange(&g_audioFailed, 1); break; }
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

// Закрыть канал, дав читателю забрать остаток, — но не ждать вечно, если тот занят другим каналом.
static DWORD WINAPI flushThread(LPVOID h) { FlushFileBuffers((HANDLE)h); return 0; }
static void closePipe(HANDLE h) {
    HANDLE t = CreateThread(nullptr, 0, flushThread, h, 0, nullptr);
    bool done = true;
    if (t) {
        done = WaitForSingleObject(t, 3000) == WAIT_OBJECT_0;
        if (!done) {
            CancelSynchronousIo(t);
            done = WaitForSingleObject(t, 500) == WAIT_OBJECT_0;
        }
        CloseHandle(t);
    }
    if (done) CloseHandle(h);   // иначе канал закроется при выходе из программы
}

int wmain(int argc, wchar_t** argv) {
    // Без этого Windows не отдаёт картинку экрана в формате HDR.
    typedef BOOL(WINAPI* SetDpiFn)(HANDLE);
    if (HMODULE user = GetModuleHandleW(L"user32.dll"))
        if (SetDpiFn fn = (SetDpiFn)GetProcAddress(user, "SetProcessDpiAwarenessContext")) fn((HANDLE)(INT_PTR)-4);   // PER_MONITOR_AWARE_V2
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
        else if (k == L"--list-audio-apps") a.listApps = true;
        else if (k == L"--window") a.window = (HWND)(INT_PTR)_wtoi64(next());
        else if (k == L"--encode") a.encode = next();
        else if (k == L"--frame") swscanf_s(next(), L"%dx%d", &a.frameW, &a.frameH);
        else if (k == L"--bitrate") a.bitrate = std::max(1, std::min(200, _wtoi(next())));
        else if (k == L"--frames") a.frames = next();
        else if (k == L"--mux") a.mux = true;
        else if (k == L"--mux-audio") a.muxAudio = true;
        else if (k == L"--audio-offset") a.audioOffset = _wtoi(next());
        else if (k == L"--tracks") a.tracks = std::max(0, std::min(8, _wtoi(next())));
        else if (k == L"--track-mic") a.trackMic = _wtoi(next());
        else if (k == L"--track-other") a.trackOther = _wtoi(next());
        else if (k == L"--include-parent") a.includeParent = true;
        else if (k == L"--track") {     // имя.exe=номер дорожки
            std::wstring spec = next();
            const size_t eq = spec.rfind(L'=');
            if (eq != std::wstring::npos && eq > 0) {
                std::wstring exe = spec.substr(0, eq);
                for (auto& c : exe) c = (wchar_t)towlower(c);
                a.appTrack[exe] = _wtoi(spec.substr(eq + 1).c_str());
            }
        }
        else if (k == L"--app-vol") {   // имя.exe=громкость
            std::wstring spec = next();
            const size_t eq = spec.rfind(L'=');
            if (eq != std::wstring::npos && eq > 0) {
                std::wstring exe = spec.substr(0, eq);
                for (auto& c : exe) c = (wchar_t)towlower(c);
                a.appVol[exe] = (float)_wtof(spec.substr(eq + 1).c_str());
            }
        }
    }
    a.fps = std::max(1, std::min(480, a.fps));
    if (a.listApps) {   // какие программы сейчас могут звучать — для списка в настройках
        CoInitializeEx(nullptr, COINIT_MULTITHREADED);
        const std::map<DWORD, ProcInfo> procs = snapshotProcesses();
        std::set<std::wstring> names;
        for (DWORD r : audioRoots(procs, GetCurrentProcessId(), a.parent)) names.insert(procs.at(r).exe);
        for (const std::wstring& n : names) printf("APP %s\n", narrow(n).c_str());
        fflush(stdout);
        return 0;
    }
    if (a.parent) CreateThread(nullptr, 0, parentWatch, (LPVOID)(ULONG_PTR)a.parent, 0, nullptr);
    SetPriorityClass(GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS);
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_HIGHEST);

    PreciseTimer timer;
    Capturer cap;
    int w = a.tw, h = a.th;

    if (!a.test && a.window) {
        // Одно окно: Windows сама отдаёт его картинку, даже если поверх что-то всплыло.
        cap.frameW = a.frameW; cap.frameH = a.frameH;
        if (a.cpu || !IsWindow(a.window) || !cap.initWindow(a.window, a.height, a.cursor)) {
            logf("error: window capture unavailable");
            return 7;   // программа перейдёт на запись монитора
        }
        const double until = now() + 2.0;
        while (!cap.haveImage && now() < until) {
            if (cap.pollWindow() == -2) return 6;
            if (!cap.haveImage) Sleep(5);
        }
        w = cap.outW;
        h = cap.outH;
    } else if (!a.test) {
        cap.wantGpu = !a.cpu;
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
            if ((cap.windowMode ? cap.pollWindow() : cap.poll()) < 0) break;
            n++;
        }
        bool dumped = false;
        if (!a.dump.empty()) {   // один готовый кадр в файл — сверить картинку в проверках
            std::vector<uint8_t> frame(frameSize, 0);
            const bool ok = nv12 ? cap.composeGpu(frame, a.cursor && !cap.windowMode) : (cap.compose(frame, a.cursor), true);
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
    const bool encoding = !a.encode.empty();
    bool hwFrames = false;   // кадры для кодировщика остаются в памяти видеокарты
    if (encoding) {
#ifdef MC_FFMPEG
        int mode = Encoder::CPU;
        if (!a.test && cap.gpu && a.frames != L"cpu") mode = a.frames == L"download" ? Encoder::DOWNLOAD : Encoder::GPU;
        g_enc.mux = a.mux;
        if (!g_enc.init(narrow(a.encode), w, h, a.fps, a.bitrate, mode, nv12, w * 4, cap.dev, cap.ctx)) return 8;
        if (a.mux && a.muxAudio && !g_enc.initAudio(1 + a.tracks, a.audioOffset)) return 8;
        hwFrames = mode != Encoder::CPU;
#else
        logf("error: this build has no encoder");
        return 8;   // программа перейдёт на кодирование через ffmpeg
#endif
    }
    g_queue.frameSize = frameSize;
    g_queue.capacity = (size_t)std::max(8, (int)(a.fps * 0.25));   // до четверти секунды кадров
    HANDLE pipe = CreateNamedPipeW(a.pipe.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1,
                                   (DWORD)std::min<size_t>(frameSize, 8u << 20), 0, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) { logf("error: CreateNamedPipe %lu", GetLastError()); return 2; }
    g_pipe = pipe;

    if (!a.audioPipe.empty()) {
        g_audioPipe = CreateNamedPipeW(a.audioPipe.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1, 1 << 16, 0, 0, nullptr);
        if (g_audioPipe == INVALID_HANDLE_VALUE) { logf("error: CreateNamedPipe(audio) %lu", GetLastError()); return 2; }
        for (int k = 1; k <= a.tracks; k++) {
            TrackPipe* t = new TrackPipe();
            const std::wstring name = a.audioPipe + L"_" + std::to_wstring(k);
            t->h = CreateNamedPipeW(name.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1, 1 << 20, 0, 0, nullptr);
            if (t->h == INVALID_HANDLE_VALUE) { logf("error: CreateNamedPipe(track) %lu", GetLastError()); return 2; }
            g_trackPipes.push_back(t);
        }
    }
    printf("READY %d %d %d %s\n", w, h, a.fps, encoding ? (a.mux ? "ts" : "coded") : (nv12 ? "nv12" : "bgra"));
    fflush(stdout);

    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
        logf("error: ConnectNamedPipe %lu", GetLastError());
        return 2;
    }
    HANDLE writer = nullptr;
    if (encoding) {
#ifdef MC_FFMPEG
        if (!g_enc.open()) return 8;
        writer = CreateThread(nullptr, 0, encodeThread, nullptr, 0, nullptr);
#endif
    } else {
        writer = CreateThread(nullptr, 0, writerThread, nullptr, 0, nullptr);
    }

    g_muxAudio = encoding && a.mux && a.muxAudio;
    const bool audio = !a.audioPipe.empty() || g_muxAudio;
    HANDLE audioWorker = nullptr;
    if (audio) {
        g_fps = a.fps;
        g_queue.onDrop = onVideoDrop;
        g_appVol = a.appVol;
        g_volSystem = a.volSystem;
        g_parentPid = a.parent;
        g_appTrack = a.appTrack;
        g_tracks = a.tracks;
        g_trackOther = a.trackOther;
        g_includeParent = a.includeParent;
        // есть своя громкость программ или отдельные дорожки — снимаем каждую программу отдельно
        g_perApp = a.audioSystem && (!a.appVol.empty() || !a.appTrack.empty());
        if (a.audioSystem && !g_perApp) { AudioSource* s = new AudioSource(); s->loopback = true; s->gain = a.volSystem; s->track = a.trackOther; g_sources.push_back(s); }
        if (!a.mic.empty()) { AudioSource* s = new AudioSource(); s->name = a.mic; s->gain = a.volMic; s->track = a.trackMic; g_sources.push_back(s); }
        audioWorker = CreateThread(nullptr, 0, audioThread, nullptr, 0, nullptr);
    }

    const double t0 = now();
    g_t0 = t0;   // с этого момента идёт общий отсчёт кадров и звука
    long long n = 0, produced = 0, trimmed = 0, lateOver2 = 0;
    QItem cur;
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
            FramePtr fresh = g_queue.acquire();
            drawTest(*fresh, w, h, now() - t0);
            const long long stamp[2] = { produced, epochMicros() };   // номер и время кадра — для проверок
            memcpy(fresh->data(), stamp, sizeof(stamp));
            cur.cpu = fresh;
        } else {
            int r = cap.windowMode ? cap.pollWindow() : cap.poll();
            if (r == -2) { logf("info: window closed"); code = 6; break; }   // окно закрыли
            if (r < 0) { code = 3; break; }
            if ((r > 0 || !cur) && hwFrames) {
#ifdef MC_FFMPEG
                // Рисуем кадр сразу в текстуру кодировщика — в обычную память он не попадает.
                ID3D11RenderTargetView* view = nullptr;
                std::shared_ptr<void> fresh = g_enc.newFrame(&view);
                if (!fresh) { code = 9; break; }
                cap.composeTo(view, a.cursor && !cap.windowMode);
                cur.hw = fresh;
#endif
            } else if (r > 0 || !cur) {
                FramePtr fresh = g_queue.acquire();
                if (nv12) {
                    if (!cap.composeGpu(*fresh, a.cursor && !cap.windowMode)) { logf("error: gpu readback failed"); code = 5; break; }
                } else {
                    cap.compose(*fresh, a.cursor);
                }
                cur.cpu = fresh;
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
        if (g_encFailed) { code = 9; break; }          // кодировщик отказал — программа выберет другой способ
        if (g_writerFailed || g_audioFailed) break;   // читатель (ffmpeg) закрылся

        const double t = now();
        if (t - statT >= a.stat) {
            const long long written = g_written;
            const LONG wmax = InterlockedExchange(&g_writeMaxMicros, 0);
            if (audio) {
                std::string info;
                { std::lock_guard<std::mutex> lock(g_audioInfoLock); info = g_audioInfo; }
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
    }
    logf("done frames=%lld elapsed=%.3f late_max=%.2fms late_over_2ms=%lld write_max=%.1fms highres_timer=%d trimmed=%lld overflow=%lld",
         (long long)g_written, now() - t0, totalMaxLate * 1000.0, lateOver2, g_writeMaxTotalMicros / 1000.0,
         timer.highRes ? 1 : 0, trimmed, g_queue.overflow);
    // Сначала закрываем видео: читатель дочитывает его до конца, после этого спокойно добирает звук.
    closePipe(pipe);
    if (g_audioPipe != INVALID_HANDLE_VALUE) closePipe(g_audioPipe);
    for (TrackPipe* t : g_trackPipes) if (t->connected) closePipe(t->h);
    cap.close();
    return code;
}

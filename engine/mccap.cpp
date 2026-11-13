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
// В stdout печатает одну строку «READY ширина высота fps», в stderr — статистику.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <mmsystem.h>

#include <algorithm>
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
};

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
        if (info.LastMouseUpdateTime.QuadPart != 0) {
            ptrVisible = info.PointerPosition.Visible != 0;
            ptr = info.PointerPosition.Position;
            pointerMoves++;
            result = 2;
            if (info.PointerShapeBufferSize > 0) {
                shape.resize(info.PointerShapeBufferSize);
                UINT need = 0;
                if (FAILED(dupl->GetFramePointerShape((UINT)shape.size(), shape.data(), &need, &shapeInfo))) shape.clear();
            }
        }
        if (info.LastPresentTime.QuadPart != 0 && res) {
            ID3D11Texture2D* tex = nullptr;
            if (SUCCEEDED(res->QueryInterface(__uuidof(ID3D11Texture2D), (void**)&tex))) {
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
        if (q.size() >= capacity) { q.pop_front(); overflow++; }   // читатель совсем встал — старое не копим
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
        double until = now() + 3.0;   // ждём первую картинку экрана
        int r = 0;
        while (!cap.haveImage && now() < until) {
            r = cap.poll();
            if (r < 0) return 3;
            if (!cap.haveImage) Sleep(5);
        }
        if (cap.width == 0) { logf("error: DuplicateOutput 0x%08lx", cap.lastError); return 2; }
        w = cap.width; h = cap.height;
    }

    if (a.probe) {
        const double t0 = now();
        long n = 0;
        while (now() - t0 < (a.seconds > 0 ? a.seconds : 3.0)) {
            timer.sleepUntil(t0 + (double)n / a.fps);
            if (cap.poll() < 0) break;
            n++;
        }
        printf("PROBE ok %dx%d images=%u pointer=%u lost=%u highres_timer=%d\n",
               w, h, cap.images, cap.pointerMoves, cap.lost, timer.highRes ? 1 : 0);
        fflush(stdout);
        cap.close();
        return 0;
    }

    if (a.pipe.empty()) { logf("error: --pipe required"); return 2; }
    const size_t frameSize = (size_t)w * h * 4;
    g_queue.frameSize = frameSize;
    g_queue.capacity = (size_t)std::max(8, (int)(a.fps * 0.25));   // до четверти секунды кадров
    HANDLE pipe = CreateNamedPipeW(a.pipe.c_str(), PIPE_ACCESS_OUTBOUND, PIPE_TYPE_BYTE | PIPE_WAIT, 1,
                                   (DWORD)std::min<size_t>(frameSize, 8u << 20), 0, 0, nullptr);
    if (pipe == INVALID_HANDLE_VALUE) { logf("error: CreateNamedPipe %lu", GetLastError()); return 2; }
    g_pipe = pipe;

    printf("READY %d %d %d\n", w, h, a.fps);
    fflush(stdout);

    if (!ConnectNamedPipe(pipe, nullptr) && GetLastError() != ERROR_PIPE_CONNECTED) {
        logf("error: ConnectNamedPipe %lu", GetLastError());
        return 2;
    }
    HANDLE writer = CreateThread(nullptr, 0, writerThread, nullptr, 0, nullptr);

    const double t0 = now();
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
                cap.compose(*cur, a.cursor);
            }
        }

        // Очередь должна быть почти пустой: кадр уходит к ffmpeg сразу, как тот готов его взять.
        // Короткие всплески — норма (ffmpeg читает порциями). А если очередь не опускается целую
        // секунду, значит, накопилось отставание картинки от звука — лишнее выбрасываем.
        const size_t depth = g_queue.depth();
        if (depth < minDepth) minDepth = depth;
        if (depth > maxDepth) maxDepth = depth;
        if (++windowCount >= a.fps) {
            if (minDepth > 2) { g_queue.trim(minDepth - 1); trimmed += (long long)(minDepth - 1); }
            minDepth = (size_t)-1;
            windowCount = 0;
        }
        g_queue.push(cur);   // нового кадра нет — повторяется прошлый, без копирования
        n++;
        produced++;
        if (g_writerFailed) break;   // читатель (ffmpeg) закрылся

        const double t = now();
        if (t - statT >= a.stat) {
            const long long written = g_written;
            const LONG wmax = InterlockedExchange(&g_writeMaxMicros, 0);
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
    logf("done frames=%lld elapsed=%.3f late_max=%.2fms late_over_2ms=%lld write_max=%.1fms highres_timer=%d trimmed=%lld overflow=%lld",
         (long long)g_written, now() - t0, totalMaxLate * 1000.0, lateOver2, g_writeMaxTotalMicros / 1000.0,
         timer.highRes ? 1 : 0, trimmed, g_queue.overflow);
    FlushFileBuffers(pipe);
    CloseHandle(pipe);
    cap.close();
    return code;
}

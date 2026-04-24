// DQSG API Traffic Logger - iOS Tweak
// Hook IL2CPP functions to log all plaintext API request/response
//
// Hook points:
//   DMHttpApi.Call           (RVA 0x5464FA8) — plaintext request
//   <>c__DisplayClass23_1.<CallMain>b__2  (RVA 0x546A088) — decrypted response
//   <Login>b__0             (RVA 0x5467E44) — stored_key
//   Lib.XorBytes            (RVA 0x54681E0) — session_key derivation

#import <substrate.h>
#import <Foundation/Foundation.h>
#import <mach-o/dyld.h>
#import <os/log.h>

#define TAG "#pc [api]"

// ============================================================
// IL2CPP helpers
// ============================================================

// Il2CppString: klass(8) + monitor(8) + length(4) + chars(UTF16)
static NSString *il2cppStr(void *s) {
    if (!s) return @"(null)";
    int32_t len = *(int32_t *)((uint8_t *)s + 0x10);
    uint16_t *chars = (uint16_t *)((uint8_t *)s + 0x14);
    return [[NSString alloc] initWithCharacters:chars length:len];
}

// Il2CppArray: klass(8) + monitor(8) + bounds(8) + max_length(4) + [pad4] + data
// max_length at 0x18, data at 0x20
static uint32_t il2cppArrLen(void *a) {
    if (!a) return 0;
    return *(uint32_t *)((uint8_t *)a + 0x18);
}

static uint8_t *il2cppArrData(void *a) {
    if (!a) return NULL;
    return (uint8_t *)a + 0x20;
}

static NSString *hexFromArr(void *a, int limit) {
    if (!a) return @"(null)";
    uint32_t len = il2cppArrLen(a);
    uint8_t *data = il2cppArrData(a);
    int n = (int)MIN(len, limit);
    NSMutableString *hex = [NSMutableString stringWithCapacity:n * 2];
    for (int i = 0; i < n; i++) [hex appendFormat:@"%02x", data[i]];
    return [NSString stringWithFormat:@"(%u bytes) %@%@", len, hex, len > limit ? @"..." : @""];
}

// ============================================================
// Hook 1: DMHttpApi.Call — plaintext request body
// RVA: 0x5464FA8
// static void Call(string path, byte[] data, Action<byte[]> onSuccess,
//                  bool auth, bool guard, bool inplace, bool skip)
// ARM64: x0=path, x1=data, x2=onSuccess, w3..w6=bools, x7=MethodInfo*
// ============================================================

typedef void (*Call_t)(void *, void *, void *, bool, bool, bool, bool, void *);
static Call_t orig_Call;

static void hook_Call(void *path, void *data, void *onSuccess,
                      bool auth, bool guard, bool inplace, bool skip, void *mi) {
    NSString *p = il2cppStr(path);
    NSString *d = hexFromArr(data, 256);
    os_log(OS_LOG_DEFAULT, TAG " REQ %{public}@ %{public}@", p, d);
    orig_Call(path, data, onSuccess, auth, guard, inplace, skip, mi);
}

// ============================================================
// Hook 2: Response — decrypted response body
// DMHttpApi.<>c__DisplayClass23_1.<CallMain>b__2
// RVA: 0x546A088
// instance void b__2()
//   this+0x10 = byte[] responseData
//   this+0x18 = <>c__DisplayClass23_0 which has:
//     +0x10 = string requestPath
// ============================================================

typedef void (*Resp_t)(void *, void *);
static Resp_t orig_Resp;

static void hook_Resp(void *self, void *mi) {
    void *responseData = *(void **)((uint8_t *)self + 0x10);
    void *outer = *(void **)((uint8_t *)self + 0x18);
    void *requestPath = outer ? *(void **)((uint8_t *)outer + 0x10) : NULL;
    NSString *p = il2cppStr(requestPath);
    NSString *d = hexFromArr(responseData, 256);
    os_log(OS_LOG_DEFAULT, TAG " RES %{public}@ %{public}@", p, d);
    orig_Resp(self, mi);
}

// ============================================================
// Hook 3: Login callback — stored_key
// DMHttpApi.<>c__DisplayClass17_0.<Login>b__0
// RVA: 0x5467E44
// instance void b__0(long userId, byte[] commonKey)
// ARM64: x0=this, x1=userId(i64), x2=commonKey, x3=MethodInfo*
// ============================================================

typedef void (*Login_t)(void *, int64_t, void *, void *);
static Login_t orig_Login;

static void hook_Login(void *self, int64_t userId, void *commonKey, void *mi) {
    NSString *k = hexFromArr(commonKey, 32);
    os_log(OS_LOG_DEFAULT, TAG " LOGIN userId=%lld stored_key=%{public}@", userId, k);
    orig_Login(self, userId, commonKey, mi);
}

// ============================================================
// Hook 4: Lib.XorBytes — catch sessionKey derivation
// RVA: 0x54681E0
// static byte[] XorBytes(byte[] l, byte[] r)
// ARM64: x0=l, x1=r, x2=MethodInfo*
// ============================================================

typedef void *(*Xor_t)(void *, void *, void *);
static Xor_t orig_Xor;

static void *hook_Xor(void *l, void *r, void *mi) {
    void *ret = orig_Xor(l, r, mi);
    if (il2cppArrLen(l) == 32 && il2cppArrLen(r) == 32) {
        NSString *lh = hexFromArr(l, 32);
        NSString *rh = hexFromArr(r, 32);
        NSString *oh = hexFromArr(ret, 32);
        os_log(OS_LOG_DEFAULT, TAG " XOR L=%{public}@ R=%{public}@ OUT=%{public}@", lh, rh, oh);
    }
    return ret;
}

// ============================================================
// Init
// ============================================================

static uintptr_t findBase(const char *name) {
    for (uint32_t i = 0; i < _dyld_image_count(); i++) {
        if (strstr(_dyld_get_image_name(i), name))
            return (uintptr_t)_dyld_get_image_header(i);
    }
    return 0;
}

%ctor {
    uintptr_t base = findBase("UnityFramework");
    if (!base) {
        os_log(OS_LOG_DEFAULT, TAG " UnityFramework not found");
        return;
    }
    os_log(OS_LOG_DEFAULT, TAG " base=0x%lx", (unsigned long)base);

    MSHookFunction((void *)(base + 0x5464FA8), (void *)hook_Call, (void **)&orig_Call);
    MSHookFunction((void *)(base + 0x546A088), (void *)hook_Resp, (void **)&orig_Resp);
    MSHookFunction((void *)(base + 0x5467E44), (void *)hook_Login, (void **)&orig_Login);
    MSHookFunction((void *)(base + 0x54681E0), (void *)hook_Xor, (void **)&orig_Xor);

    os_log(OS_LOG_DEFAULT, TAG " 4 hooks installed");
}

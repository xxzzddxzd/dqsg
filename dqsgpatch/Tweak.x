#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <dlfcn.h>
#import <mach-o/dyld.h>
#import <sys/sysctl.h>
#import <substrate.h>
#import <pthread.h>
#import <objc/runtime.h>
#import <signal.h>
#import <sys/ucontext.h>
#import <stdatomic.h>

// IL2CPP object layout helpers
typedef struct {
    void *klass;
    void *monitor;
    void *bounds;
    uint64_t max_length;
    uint8_t items[];
} Il2CppByteArray;

typedef struct {
    void *klass;
    void *monitor;
    int32_t length;
    uint16_t chars[];
} Il2CppString;

// ============================================================
// A. NSURLProtocol — intercept gameguard network requests
// ============================================================

@interface PCGuardProtocol : NSURLProtocol
@end

@implementation PCGuardProtocol

+ (BOOL)canInitWithRequest:(NSURLRequest *)request {
    NSString *host = request.URL.host;
    if (!host) return NO;
    if ([host hasSuffix:@"gameguard.jp"]) {
        if ([NSURLProtocol propertyForKey:@"PCHandled" inRequest:request]) return NO;
        return YES;
    }
    return NO;
}

+ (NSURLRequest *)canonicalRequestForRequest:(NSURLRequest *)request {
    return request;
}

- (void)sendResponse:(NSData *)data statusCode:(NSInteger)code {
    NSHTTPURLResponse *response = [[NSHTTPURLResponse alloc]
        initWithURL:self.request.URL
        statusCode:code
        HTTPVersion:@"HTTP/1.1"
        headerFields:@{@"Content-Type": @"text/plain"}];
    [self.client URLProtocol:self didReceiveResponse:response cacheStoragePolicy:NSURLCacheStorageNotAllowed];
    [self.client URLProtocol:self didLoadData:data];
    [self.client URLProtocolDidFinishLoading:self];
}

- (void)startLoading {
    NSString *path = self.request.URL.path ?: @"";
    NSString *host = self.request.URL.host ?: @"";

    if ([path isEqualToString:@"/api/chkApp"]) {
        NSLog(@"#pc [net] chkApp -> mock success");
        [self sendResponse:[@"{\"message\":\"ok\",\"status\":200}" dataUsingEncoding:NSUTF8StringEncoding] statusCode:200];
    } else if ([path hasPrefix:@"/api/"]) {
        NSLog(@"#pc [net] %@ -> mock success", path);
        [self sendResponse:[@"{\"message\":\"ok\",\"status\":200}" dataUsingEncoding:NSUTF8StringEncoding] statusCode:200];
    } else if ([path isEqualToString:@"/npggm/service.do"]) {
        uint32_t token = arc4random_uniform(90000000) + 10000000;
        NSLog(@"#pc [net] service.do -> mock token %u", token);
        [self sendResponse:[[NSString stringWithFormat:@"%u", token] dataUsingEncoding:NSUTF8StringEncoding] statusCode:200];
    } else if ([path isEqualToString:@"/doAuth2"] || [path isEqualToString:@"/iam"]) {
        static atomic_int authCount = 0;
        if (atomic_fetch_add(&authCount, 1) == 0) {
            NSLog(@"#pc [net] %@%@ -> hung (no response, future logs suppressed)", host, path);
        }
        return;
    } else {
        NSLog(@"#pc [net] %@%@ -> empty", host, path);
        [self sendResponse:[NSData data] statusCode:200];
    }
}

- (void)stopLoading {}

@end

// Hook NSURLSessionConfiguration to inject our protocol
%hook NSURLSessionConfiguration

- (NSArray *)protocolClasses {
    NSMutableArray *classes = [NSMutableArray arrayWithObject:[PCGuardProtocol class]];
    NSArray *orig = %orig;
    if (orig) [classes addObjectsFromArray:orig];
    return classes;
}

%end

// Override timeout for gameguard requests so they hang forever instead of retrying every 10s
%hook NSURLRequest

- (instancetype)initWithURL:(NSURL *)URL cachePolicy:(NSURLRequestCachePolicy)cachePolicy timeoutInterval:(NSTimeInterval)timeoutInterval {
    if (URL.absoluteString && ![URL.host hasSuffix:@"gameguard.jp"]) {
        NSLog(@"#pc [net] request URL: %@", URL.absoluteString);
    }
    return %orig;
}

- (instancetype)initWithURL:(NSURL *)URL {
    if (URL.absoluteString && ![URL.host hasSuffix:@"gameguard.jp"]) {
        NSLog(@"#pc [net] request URL: %@", URL.absoluteString);
    }
    return %orig;
}

- (NSTimeInterval)timeoutInterval {
    NSString *host = self.URL.host;
    if (host && [host hasSuffix:@"gameguard.jp"]) {
        NSString *path = self.URL.path ?: @"";
        if ([path isEqualToString:@"/doAuth2"] || [path isEqualToString:@"/iam"]) {
            return 999999;
        }
    }
    return %orig;
}

%end

// ============================================================
// B. C/C++ Function Hooks — process/signal control
// ============================================================

static pid_t (*orig_fork)(void);
static pid_t hook_fork(void) { errno = ENOSYS; return -1; }

static void (*orig_exit)(int status);
static void hook_exit(int status) {
    if (pthread_main_np()) { orig_exit(status); }
    NSLog(@"#pc [exit] BLOCKED exit(%d) from background thread", status);
    pthread_exit(NULL);
}

static void (*orig__exit)(int status);
static void hook__exit(int status) {
    if (pthread_main_np()) { orig__exit(status); }
    pthread_exit(NULL);
}

static void (*orig_abort)(void);
static void hook_abort(void) {
    if (pthread_main_np()) { orig_abort(); }
    pthread_exit(NULL);
}

static int (*orig_kill)(pid_t pid, int sig);
static int hook_kill(pid_t pid, int sig) {
    if (pid == getpid() || pid == 0 || pid == -1) { return 0; }
    return orig_kill(pid, sig);
}

static int (*orig_raise)(int sig);
static int hook_raise(int sig) { return 0; }

static int (*orig_pthread_kill)(pthread_t thread, int sig);
static int hook_pthread_kill(pthread_t thread, int sig) { return 0; }

static int (*orig_sysctl)(int *name, u_int namelen, void *oldp, size_t *oldlenp, void *newp, size_t newlen);
static int hook_sysctl(int *name, u_int namelen, void *oldp, size_t *oldlenp, void *newp, size_t newlen) {
    int ret = orig_sysctl(name, namelen, oldp, oldlenp, newp, newlen);
    if (ret == 0 && namelen == 4 &&
        name[0] == CTL_KERN && name[1] == KERN_PROC && name[2] == KERN_PROC_PID && name[3] == getpid()) {
        struct kinfo_proc *info = (struct kinfo_proc *)oldp;
        if (info) {
            info->kp_proc.p_flag &= ~P_TRACED;
        }
    }
    return ret;
}

// ============================================================
// C. Runtime ObjC swizzling
// ============================================================

static BOOL hook_jailBroken_false(id self, SEL _cmd) { return NO; }
static BOOL hook_isJailbroken_false(id self, SEL _cmd) { return NO; }
static BOOL hook_isRooted_false(id self, SEL _cmd) { return NO; }

static void swizzleAllJBSelectors() {
    SEL sels[] = {
        NSSelectorFromString(@"jailBroken"),
        NSSelectorFromString(@"isJailbroken"),
        NSSelectorFromString(@"isRooted"),
    };
    IMP imps[] = {
        (IMP)hook_jailBroken_false,
        (IMP)hook_isJailbroken_false,
        (IMP)hook_isRooted_false,
    };

    int numClasses = objc_getClassList(NULL, 0);
    if (numClasses <= 0) return;
    Class *classes = (Class *)malloc(sizeof(Class) * numClasses);
    objc_getClassList(classes, numClasses);

    for (int i = 0; i < numClasses; i++) {
        for (int s = 0; s < 3; s++) {
            Method m = class_getInstanceMethod(classes[i], sels[s]);
            if (m) {
                char retType[8];
                method_getReturnType(m, retType, sizeof(retType));
                if (retType[0] == 'B' || retType[0] == 'c') {
                    method_setImplementation(m, imps[s]);
                }
            }
        }
    }
    free(classes);
}

// ============================================================
// D. Signal handler — catch deliberate SIGSEGV/SIGBUS crashes
// ============================================================

static void pc_crash_handler(int sig, siginfo_t *info, void *uap) {
    uintptr_t fault_addr = (uintptr_t)info->si_addr;
    if (fault_addr < 0x1000) {
        ucontext_t *uc = (ucontext_t *)uap;
        uintptr_t lr = uc->uc_mcontext->__ss.__lr;
        NSLog(@"#pc [sig] caught %s at 0x%lx, redirecting PC to LR 0x%lx",
              sig == SIGSEGV ? "SIGSEGV" : "SIGBUS", (unsigned long)fault_addr, (unsigned long)lr);
        uc->uc_mcontext->__ss.__pc = lr;
        return;
    }
    signal(sig, SIG_DFL);
    raise(sig);
}

static void installCrashHandlers() {
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = pc_crash_handler;
    sa.sa_flags = SA_SIGINFO;
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
    NSLog(@"#pc [init] SIGSEGV/SIGBUS handler installed");
}

static int (*orig_sigaction)(int sig, const struct sigaction *act, struct sigaction *oact);
static int hook_sigaction(int sig, const struct sigaction *act, struct sigaction *oact) {
    if (act && (sig == SIGSEGV || sig == SIGBUS)) {
        NSLog(@"#pc [sig] BLOCKED sigaction(%d) override attempt", sig);
        if (oact) {
            struct sigaction current;
            orig_sigaction(sig, NULL, &current);
            *oact = current;
        }
        return 0;
    }
    return orig_sigaction(sig, act, oact);
}

// ============================================================
// E. IL2CPP Crypto Dump — intercept EncryptRequest/DecryptResponse
// ============================================================

static uintptr_t g_unityBase = 0;
static atomic_int g_reqSeq = 0;
static atomic_int g_resSeq = 0;

static NSString *hexFromBytes(const uint8_t *bytes, int len) {
    if (!bytes || len <= 0) return @"(null)";
    NSMutableString *s = [NSMutableString stringWithCapacity:len * 2];
    int limit = len > 64 ? 64 : len;
    for (int i = 0; i < limit; i++) [s appendFormat:@"%02x", bytes[i]];
    if (len > 64) [s appendFormat:@"...(%d bytes total)", len];
    return s;
}

static NSString *hexFromBytesFull(const uint8_t *bytes, int len) {
    if (!bytes || len <= 0) return @"(null)";
    NSMutableString *s = [NSMutableString stringWithCapacity:len * 2];
    for (int i = 0; i < len; i++) [s appendFormat:@"%02x", bytes[i]];
    return s;
}

static void writeByteArrayToTmpWithPrefix(Il2CppByteArray *arr, NSString *prefix, int seq) {
    if (!arr || arr->max_length == 0) return;
    NSString *tmpDir = NSTemporaryDirectory();
    NSString *filePath = [tmpDir stringByAppendingPathComponent:[NSString stringWithFormat:@"%@_%d", prefix, seq]];
    NSData *data = [NSData dataWithBytes:arr->items length:(NSUInteger)arr->max_length];
    [data writeToFile:filePath atomically:YES];
    NSLog(@"#pc [crypto] written to %@", filePath);
}

static NSString *il2cppStringToNS(Il2CppString *str) {
    if (!str || str->length <= 0) return @"(null)";
    return [[NSString alloc] initWithCharacters:(const unichar *)str->chars length:str->length];
}

static void logByteArray(const char *label, Il2CppByteArray *arr) {
    if (!arr) { NSLog(@"#pc [crypto] %s = (null)", label); return; }
    NSLog(@"#pc [crypto] %s (%llu bytes) = %@", label, arr->max_length, hexFromBytes(arr->items, (int)arr->max_length));
}

// SVAPI.get_StartupKey — RVA 0x54A8650 (1.0.3)
typedef Il2CppByteArray* (*GetStartupKey_t)(void *method);
static GetStartupKey_t orig_GetStartupKey;
static Il2CppByteArray* hook_GetStartupKey(void *method) {
    Il2CppByteArray *key = orig_GetStartupKey(method);
    static atomic_int logged = 0;
    if (atomic_fetch_add(&logged, 1) == 0) {
        logByteArray("StartupKey", key);
    }
    return key;
}

// DMCryptography.EncryptRequest — RVA 0x5463148 (1.0.3)
typedef Il2CppByteArray* (*EncryptRequest_t)(Il2CppByteArray *sessionKey, Il2CppString *requestPath, Il2CppByteArray *data, void *method);
static EncryptRequest_t orig_EncryptRequest;
static Il2CppByteArray* hook_EncryptRequest(Il2CppByteArray *sessionKey, Il2CppString *requestPath, Il2CppByteArray *data, void *method) {
    int seq = atomic_fetch_add(&g_reqSeq, 1);
    NSString *path = il2cppStringToNS(requestPath);
    NSLog(@"#pc [crypto] === EncryptRequest #%d ===", seq);
    NSLog(@"#pc [crypto] path: %@", path);
    logByteArray("sessionKey", sessionKey);
    if (data) {
        NSString *fullHex = hexFromBytesFull(data->items, (int)data->max_length);
        NSLog(@"#pc [crypto] plaintext (%llu bytes) = %@", data->max_length, fullHex);
    } else {
        NSLog(@"#pc [crypto] plaintext = (null)");
    }
    writeByteArrayToTmpWithPrefix(data, @"req", seq);
    Il2CppByteArray *result = orig_EncryptRequest(sessionKey, requestPath, data, method);
    logByteArray("encrypted", result);
    return result;
}

// DMCryptography.DecryptResponse — RVA 0x5463A70 (1.0.3)
typedef bool (*DecryptResponse_t)(Il2CppByteArray *sessionKey, Il2CppString *requestPath, Il2CppByteArray *responseData, Il2CppByteArray **decrypted, void *method);
static DecryptResponse_t orig_DecryptResponse;
static bool hook_DecryptResponse(Il2CppByteArray *sessionKey, Il2CppString *requestPath, Il2CppByteArray *responseData, Il2CppByteArray **decrypted, void *method) {
    int seq = atomic_fetch_add(&g_resSeq, 1);
    NSString *path = il2cppStringToNS(requestPath);
    bool ok = orig_DecryptResponse(sessionKey, requestPath, responseData, decrypted, method);
    NSLog(@"#pc [crypto] === DecryptResponse #%d ===", seq);
    NSLog(@"#pc [crypto] path: %@, success: %d", path, ok);
    logByteArray("sessionKey", sessionKey);
    if (ok && decrypted && *decrypted) {
        logByteArray("decrypted", *decrypted);
        writeByteArrayToTmpWithPrefix(*decrypted, @"res", seq);
    }
    return ok;
}

// DMCryptography.RandomBytes — RVA 0x5462A1C (1.0.3)
typedef Il2CppByteArray* (*RandomBytes_t)(int len, void *method);
static RandomBytes_t orig_RandomBytes;
static Il2CppByteArray* hook_RandomBytes(int len, void *method) {
    Il2CppByteArray *result = orig_RandomBytes(len, method);
    NSLog(@"#pc [crypto] RandomBytes(%d) = %@", len, hexFromBytes(result->items, (int)result->max_length));
    return result;
}

// DMCryptography.PublicEncrypt — RVA 0x5462AC4 (1.0.3)
typedef Il2CppByteArray* (*PublicEncrypt_t)(Il2CppByteArray *data, void *method);
static PublicEncrypt_t orig_PublicEncrypt;
static Il2CppByteArray* hook_PublicEncrypt(Il2CppByteArray *data, void *method) {
    NSLog(@"#pc [crypto] === PublicEncrypt ===");
    logByteArray("RSA input", data);
    Il2CppByteArray *result = orig_PublicEncrypt(data, method);
    logByteArray("RSA output", result);
    return result;
}

// DMCryptography.HmacSha1 — RVA 0x5462B58 (1.0.3)
typedef Il2CppByteArray* (*HmacSha1_t)(void *dataStream, Il2CppByteArray *key, void *method);
static HmacSha1_t orig_HmacSha1;
static Il2CppByteArray* hook_HmacSha1(void *dataStream, Il2CppByteArray *key, void *method) {
    logByteArray("HmacSha1 key", key);
    return orig_HmacSha1(dataStream, key, method);
}

// DMHttpApi.<>c__DisplayClass17_0.<Login>b__0 — RVA 0x5468144 (1.0.3)
typedef void (*LoginCallback_t)(void *thisObj, int64_t userId, Il2CppByteArray *commonKey, void *method);
static LoginCallback_t orig_LoginCallback;
static void hook_LoginCallback(void *thisObj, int64_t userId, Il2CppByteArray *commonKey, void *method) {
    NSLog(@"#pc [crypto] ===== Login callback =====");
    NSLog(@"#pc [crypto] userId: %lld", (long long)userId);
    logByteArray("commonKey (stored_key)", commonKey);
    orig_LoginCallback(thisObj, userId, commonKey, method);
}

// Lib.XorBytes — RVA 0x54684E0 (1.0.3)
typedef Il2CppByteArray* (*XorBytes_t)(Il2CppByteArray *l, Il2CppByteArray *r, void *method);
static XorBytes_t orig_XorBytes;
static Il2CppByteArray* hook_XorBytes(Il2CppByteArray *l, Il2CppByteArray *r, void *method) {
    Il2CppByteArray *result = orig_XorBytes(l, r, method);
    if (l && r && l->max_length == 32 && r->max_length == 32) {
        NSLog(@"#pc [crypto] === XorBytes (key derivation) ===");
        logByteArray("L", l);
        logByteArray("R", r);
        logByteArray("=", result);
    }
    return result;
}

// DMCryptography.SessionDecrypt — RVA 0x5462C60 (1.0.3)
typedef Il2CppByteArray* (*SessionDecrypt_t)(Il2CppByteArray *data, void *method);
static SessionDecrypt_t orig_SessionDecrypt;
static Il2CppByteArray* hook_SessionDecrypt(Il2CppByteArray *data, void *method) {
    NSLog(@"#pc [crypto] === SessionDecrypt ===");
    logByteArray("SessionDecrypt input", data);
    Il2CppByteArray *result = orig_SessionDecrypt(data, method);
    logByteArray("SessionDecrypt output", result);
    return result;
}

static void onImageLoaded(const struct mach_header *mh, intptr_t slide) {
    if (g_unityBase) return;
    Dl_info info;
    if (!dladdr((void *)mh, &info) || !info.dli_fname) return;
    if (!strstr(info.dli_fname, "UnityFramework")) return;

    g_unityBase = (uintptr_t)mh;
    NSLog(@"#pc [crypto] UnityFramework loaded at 0x%lx (slide 0x%lx)", (unsigned long)g_unityBase, (long)slide);

    MSHookFunction((void *)(g_unityBase + 0x54A8650), (void *)hook_GetStartupKey, (void **)&orig_GetStartupKey);
    MSHookFunction((void *)(g_unityBase + 0x5463148), (void *)hook_EncryptRequest, (void **)&orig_EncryptRequest);
    MSHookFunction((void *)(g_unityBase + 0x5463A70), (void *)hook_DecryptResponse, (void **)&orig_DecryptResponse);
    MSHookFunction((void *)(g_unityBase + 0x5462A1C), (void *)hook_RandomBytes, (void **)&orig_RandomBytes);
    MSHookFunction((void *)(g_unityBase + 0x5462AC4), (void *)hook_PublicEncrypt, (void **)&orig_PublicEncrypt);
    MSHookFunction((void *)(g_unityBase + 0x5462B58), (void *)hook_HmacSha1, (void **)&orig_HmacSha1);
    MSHookFunction((void *)(g_unityBase + 0x5468144), (void *)hook_LoginCallback, (void **)&orig_LoginCallback);
    MSHookFunction((void *)(g_unityBase + 0x54684E0), (void *)hook_XorBytes, (void **)&orig_XorBytes);
    MSHookFunction((void *)(g_unityBase + 0x5462C60), (void *)hook_SessionDecrypt, (void **)&orig_SessionDecrypt);

    NSLog(@"#pc [crypto] hooks installed (EncryptRequest, DecryptResponse, StartupKey, HmacSha1, PublicEncrypt, LoginCallback, XorBytes, SessionDecrypt)");
}

// ============================================================
// Constructor
// ============================================================

%ctor {
    NSLog(@"#pc ========================================");
    NSLog(@"#pc DQSG Patch v17 loaded");
    NSLog(@"#pc ========================================");

    MSHookFunction((void *)fork, (void *)hook_fork, (void **)&orig_fork);
    MSHookFunction((void *)sysctl, (void *)hook_sysctl, (void **)&orig_sysctl);
    MSHookFunction((void *)exit, (void *)hook_exit, (void **)&orig_exit);
    MSHookFunction((void *)_exit, (void *)hook__exit, (void **)&orig__exit);
    MSHookFunction((void *)abort, (void *)hook_abort, (void **)&orig_abort);
    MSHookFunction((void *)kill, (void *)hook_kill, (void **)&orig_kill);
    MSHookFunction((void *)raise, (void *)hook_raise, (void **)&orig_raise);
    MSHookFunction((void *)pthread_kill, (void *)hook_pthread_kill, (void **)&orig_pthread_kill);
    MSHookFunction((void *)sigaction, (void *)hook_sigaction, (void **)&orig_sigaction);
    NSLog(@"#pc [init] C hooks installed");

    installCrashHandlers();

    swizzleAllJBSelectors();
    NSLog(@"#pc [init] ObjC swizzle complete");

    [NSURLProtocol registerClass:[PCGuardProtocol class]];
    NSLog(@"#pc [init] NSURLProtocol registered for gameguard interception");

    _dyld_register_func_for_add_image(onImageLoaded);
    NSLog(@"#pc [init] registered dyld callback for UnityFramework crypto hooks");
}

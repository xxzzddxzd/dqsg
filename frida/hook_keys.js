// Frida script: hook IL2CPP to print login keys
// Usage: frida -U -f <bundle_id> -l hook_keys.js

var moduleName = "UnityFramework";

function hexdump_bytes(ptr, len) {
    var buf = ptr.readByteArray(len);
    return Array.from(new Uint8Array(buf)).map(b => ('0' + b.toString(16)).slice(-2)).join('');
}

function readIl2cppArray(arrayPtr) {
    // il2cpp Array layout: klass(ptr), monitor(ptr), max_length(uint64), data...
    if (arrayPtr.isNull()) return null;
    var len = arrayPtr.add(Process.pointerSize * 2 + 4).readU32();
    // On 64-bit: offset to first element = 0x20 (klass:8 + monitor:8 + bounds:8 + max_length:8)
    // Simplified: data starts at offset 0x20
    var dataPtr = arrayPtr.add(0x20);
    return { ptr: dataPtr, length: len };
}

function hookOnLoaded() {
    var base = Module.findBaseAddress(moduleName);
    if (!base) {
        console.log("[!] " + moduleName + " not loaded yet");
        return false;
    }
    console.log("[*] " + moduleName + " base: " + base);

    // DMHttpApi.<>c__DisplayClass17_0.<Login>b__0(this, long userId, byte[] commonKey)
    // RVA: 0x5466274
    var loginCallback = base.add(0x5466274);
    Interceptor.attach(loginCallback, {
        onEnter: function(args) {
            // args[0] = this, args[1] = userId (long, 64-bit), args[2] = commonKey (byte[])
            var userId = args[1].toInt32();  // low 32 bits
            console.log("\n[*] ===== Login callback =====");
            console.log("[*] userId (low32): " + userId);

            var commonKeyArr = readIl2cppArray(args[2]);
            if (commonKeyArr && commonKeyArr.length > 0) {
                var hex = hexdump_bytes(commonKeyArr.ptr, commonKeyArr.length);
                console.log("[*] commonKey (stored_key): " + hex);
                console.log("[*] commonKey length: " + commonKeyArr.length);
            } else {
                console.log("[!] commonKey is null or empty");
            }
        }
    });
    console.log("[+] Hooked <Login>b__0 at " + loginCallback);

    // Lib.XorBytes(byte[] l, byte[] r) -> byte[]
    // RVA: 0x5466610
    var xorBytes = base.add(0x5466610);
    Interceptor.attach(xorBytes, {
        onEnter: function(args) {
            this.l = args[0];
            this.r = args[1];
        },
        onLeave: function(retval) {
            var lArr = readIl2cppArray(this.l);
            var rArr = readIl2cppArray(this.r);
            var resArr = readIl2cppArray(retval);
            if (lArr && rArr && resArr && lArr.length === 32) {
                console.log("\n[*] XorBytes:");
                console.log("  L: " + hexdump_bytes(lArr.ptr, lArr.length));
                console.log("  R: " + hexdump_bytes(rArr.ptr, rArr.length));
                console.log("  =: " + hexdump_bytes(resArr.ptr, resArr.length));
            }
        }
    });
    console.log("[+] Hooked Lib.XorBytes at " + xorBytes);

    return true;
}

// Try immediately, retry if module not loaded yet
if (!hookOnLoaded()) {
    var interval = setInterval(function() {
        if (hookOnLoaded()) {
            clearInterval(interval);
        }
    }, 500);
}

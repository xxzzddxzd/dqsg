#!/usr/bin/env python3
"""
DQSG chlz 抓包分析工具

用法:
    # 基本: 解密所有能解的请求 (StartupKey + loginKey)
    python3 analyze_chlz.py <chlz文件路径>

    # 提供 sessionKey 以解密登录后的请求
    python3 analyze_chlz.py <chlz文件路径> --session-key <hex>

    # 从 crypto log 自动提取 sessionKey
    python3 analyze_chlz.py <chlz文件路径> --crypto-log <log文件>

    # 只看某些请求
    python3 analyze_chlz.py <chlz文件路径> --index 4,5,7

    # 导出解密后的二进制到目录
    python3 analyze_chlz.py <chlz文件路径> --export-dir /tmp/decrypted

    # 指定账号
    python3 analyze_chlz.py <chlz文件路径> --account 1

chlz 文件格式:
    ZIP 压缩包，内含按序号组织的请求/响应对:
      N-meta.json   元数据 (JSON, 含 host/path/query/method 等)
      N-req.json    加密的请求体 (二进制, 名字虽是.json实际是二进制)
      N-res.bin     加密的响应体 (游戏API)
      N-res.dat     非加密响应 (CDN 元数据等)
      N-res.db      数据库文件 (masterdata 等)
      N-res.json    JSON 响应 (Firebase 等外部API)

加密协议:
    [16-byte IV][AES-256-CBC(Deflate(plaintext))][20-byte HMAC-SHA1]
    HMAC = HMAC-SHA1(key, IV + ciphertext + UTF8(requestPath))
    requestPath 不含 /ep01000 前缀，格式: /endpoint?query_params

密钥层级:
    StartupKey (硬编码) → 用于 masterdata/*, terms/get_terms_eu, login/startup
    loginKey = storedKey XOR StartupKey → 用于 login/login
    sessionKey = loginRandom XOR resp.SessionKey → 用于登录后所有请求

    storedKey = startupRandom XOR AuthorizationKey (存在设备 keychain)
"""

import struct
import zlib
import hmac
import hashlib
import os
import sys
import json
import zipfile
import argparse
import tempfile
import re
import shutil
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# ==============================================================================
# 常量和账号凭据
# ==============================================================================

STARTUP_KEY = bytes.fromhex(
    "5d0c9d1a34076488136525cc60e39581f8a9134374d2983400a50a6879c82498"
)

ACCOUNTS = {
    1: {
        "name": "Account 1 (old)",
        "user_id": 87125091589,
        "stored_key": bytes.fromhex("7a2216ecb295277fc270209284894853145c1874f0534c9214fa414d89a93cad"),
    },
    2: {
        "name": "Account 2 (new)",
        "user_id": 36183493676,
        "stored_key": bytes.fromhex("761a2fa55bbc43f1c3c6e95e0e14077abb71073348e5841c6f753afba417fd91"),
    },
    3: {
        "name": "Account 3 (tutorial)",
        "user_id": 62925351098,
        "stored_key": bytes.fromhex("cba9ef8a631614a113ccb50e929f4fceff57366f5816910587438741beb6956c"),
    },
}


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


# ==============================================================================
# 加解密
# ==============================================================================

def decrypt_payload(key: bytes, request_path: str, data: bytes) -> bytes:
    """解密 [16-byte IV][AES-CBC ciphertext][20-byte HMAC-SHA1]"""
    if len(data) < 36:
        raise ValueError(f"数据太短 ({len(data)} bytes), 最少需要 36 (IV+1block+HMAC)")
    iv = data[:16]
    mac = data[-20:]
    ct = data[16:-20]

    hmac_input = iv + ct + request_path.encode("utf-8")
    expected_mac = hmac.new(key, hmac_input, hashlib.sha1).digest()
    if mac != expected_mac:
        raise ValueError("HMAC 不匹配")

    cipher = AES.new(key, AES.MODE_CBC, iv)
    compressed = unpad(cipher.decrypt(ct), 16)
    if not compressed:
        return b""
    return zlib.decompress(compressed, -15)


def check_hmac(key: bytes, request_path: str, data: bytes) -> bool:
    """只验证 HMAC, 不解密"""
    if len(data) < 36:
        return False
    iv = data[:16]
    mac = data[-20:]
    ct = data[16:-20]
    hmac_input = iv + ct + request_path.encode("utf-8")
    expected = hmac.new(key, hmac_input, hashlib.sha1).digest()
    return mac == expected


# ==============================================================================
# BytesReader — 二进制协议解析
# ==============================================================================

class BytesReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_bool(self) -> bool:
        v = self.data[self.pos] != 0
        self.pos += 1
        return v

    def read_int(self) -> int:
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_uint(self) -> int:
        v = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def read_long(self) -> int:
        v = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return v

    def read_string(self) -> str:
        length = self.read_int()
        if length < 0 or length > len(self.data) - self.pos:
            raise ValueError(f"字符串长度异常: {length}")
        s = self.data[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return s

    def read_bytes(self) -> bytes:
        length = self.read_int()
        if length < 0 or length > len(self.data) - self.pos:
            raise ValueError(f"字节数组长度异常: {length}")
        b = self.data[self.pos:self.pos + length]
        self.pos += length
        return b

    def read_nullable_string(self):
        has_value = self.read_bool()
        if not has_value:
            return None
        return self.read_string()

    def read_nullable_bool(self):
        has_value = self.read_bool()
        if not has_value:
            return None
        return self.read_bool()

    def read_nullable_long(self):
        has_value = self.read_bool()
        if not has_value:
            return None
        return self.read_long()

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def peek_hex(self, n=64) -> str:
        end = min(self.pos + n, len(self.data))
        return self.data[self.pos:end].hex()


# ==============================================================================
# 已知请求/响应解析器
# ==============================================================================

KNOWN_PARSERS = {}


def parser(endpoint):
    def decorator(func):
        KNOWN_PARSERS[endpoint] = func
        return func
    return decorator


@parser("masterdata/get_version")
def parse_masterdata(data, direction):
    if direction == "request":
        return {"_note": "空请求体" if len(data) == 0 else f"{len(data)} bytes"}
    r = BytesReader(data)
    return {
        "status": r.read_int(),
        "timestamp": r.read_int(),
        "revision": r.read_int(),
        "version": r.read_string(),
    }


@parser("login/startup")
def parse_startup(data, direction):
    r = BytesReader(data)
    if direction == "request":
        mask = r.read_bytes()
        client_uuid = r.read_string()
        terminal_id = r.read_string()
        return {
            "mask": f"({len(mask)} bytes RSA) {mask.hex()[:32]}...",
            "clientUuid": client_uuid,
            "terminalId": terminal_id,
        }
    return {
        "status": r.read_int(),
        "UserId": r.read_long(),
        "AuthorizationKey": r.read_bytes().hex(),
    }


@parser("login/login")
def parse_login(data, direction):
    r = BytesReader(data)
    if direction == "request":
        auth_count = r.read_int()
        mask = r.read_bytes()
        client_uuid = r.read_string()
        advertising_id = r.read_nullable_string()
        is_tracking = r.read_nullable_bool()
        return {
            "authCount": auth_count,
            "mask": f"({len(mask)} bytes RSA) {mask.hex()[:32]}...",
            "clientUuid": client_uuid,
            "advertisingId": advertising_id,
            "isTracking": is_tracking,
        }
    status = r.read_int()
    auth_count = r.read_int()
    session_key = r.read_bytes()
    client_id = r.read_string()
    in_game_session_id = r.read_nullable_long()
    perf_metrics = r.read_bool()
    asset_cdn_url = r.read_string()
    result = {
        "status": status,
        "AuthorizationCount": auth_count,
        "SessionKey": f"({len(session_key)}B) {session_key.hex()}" if session_key else "(empty)",
        "ClientId": client_id,
        "InGameSessionId": in_game_session_id,
        "PerformanceMetricsEnabled": perf_metrics,
        "AssetCdnUrl": asset_cdn_url,
    }
    if r.remaining() > 0:
        result["_remaining"] = r.remaining()
    return result


@parser("home/fetch_info")
def parse_home_fetch_info(data, direction):
    if direction == "request":
        r = BytesReader(data)
        result = {}
        has_dt = r.read_bool()
        result["hasDeviceToken"] = has_dt
        if has_dt:
            result["DeviceToken"] = r.read_string()[:40] + "..."
        result["DeviceName"] = r.read_string()
        result["AdvertisingId"] = r.read_nullable_string()
        result["IsTrackingEnabled"] = r.read_nullable_bool()
        result["FirebaseAnalyticsInstanceId"] = r.read_nullable_string()
        result["AdjustDeviceId"] = r.read_nullable_string()
        return result
    r = BytesReader(data)
    return {"status": r.read_int(), "_size": len(data), "_note": "大型响应, 含 UserModel 等"}


@parser("terms/get_terms_eu")
def parse_terms_get(data, direction):
    if direction == "request":
        return {"_note": "空请求体" if len(data) == 0 else f"{len(data)} bytes"}
    r = BytesReader(data)
    status = r.read_int()
    html_len = r.read_int()
    return {"status": status, "html_length": html_len, "_note": "HTML 条款内容"}


@parser("terms/terms_agree_eu")
def parse_terms_agree(data, direction):
    if direction == "request":
        if len(data) == 0:
            return {"_note": "空请求体"}
        r = BytesReader(data)
        return {"version1": r.read_int(), "version2": r.read_int(), "flag": r.read_bool()}
    r = BytesReader(data)
    return {"status": r.read_int(), "_size": len(data)}


@parser("billing/update_web_store")
def parse_billing(data, direction):
    if direction == "request":
        return {"_note": "空请求体" if len(data) == 0 else f"{len(data)} bytes"}
    r = BytesReader(data)
    return {"status": r.read_int(), "_size": len(data)}


# ==============================================================================
# Crypto log 解析
# ==============================================================================

def extract_session_key_from_log(log_path: str) -> bytes | None:
    """从 tweak crypto log 中提取 sessionKey"""
    text = open(log_path).read()
    xor_calls = []
    current_l = current_r = current_result = None

    for line in text.split("\n"):
        if "XorBytes L" in line:
            m = re.search(r'= ([0-9a-f]{64})', line)
            if m:
                current_l = bytes.fromhex(m.group(1))
        elif "XorBytes R" in line:
            m = re.search(r'= ([0-9a-f]{64})', line)
            if m:
                current_r = bytes.fromhex(m.group(1))
        elif "XorBytes result" in line:
            m = re.search(r'= ([0-9a-f]{64})', line)
            if m:
                current_result = bytes.fromhex(m.group(1))
            if current_l and current_r and current_result:
                xor_calls.append((current_l, current_r, current_result))
            current_l = current_r = current_result = None

    # 最后一组 32-byte XorBytes 通常是 loginRandom XOR resp.SessionKey = sessionKey
    if len(xor_calls) >= 2:
        session_key = xor_calls[-1][2]
        print(f"  从 crypto log 提取 sessionKey: {session_key.hex()[:16]}...")
        return session_key
    return None


# ==============================================================================
# 主分析逻辑
# ==============================================================================

def extract_chlz(chlz_path: str, extract_dir: str = None) -> str:
    """解压 chlz 到临时目录，返回目录路径"""
    if extract_dir is None:
        extract_dir = tempfile.mkdtemp(prefix="chlz_")
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(chlz_path, 'r') as z:
        z.extractall(extract_dir)
    return extract_dir


def discover_requests(extract_dir: str) -> list[dict]:
    """发现所有请求，返回按序号排序的列表"""
    requests = []
    seen = set()
    for f in os.listdir(extract_dir):
        m = re.match(r'^(\d+)-meta\.json$', f)
        if not m:
            continue
        idx = int(m.group(1))
        if idx in seen:
            continue
        seen.add(idx)

        meta = json.load(open(os.path.join(extract_dir, f)))
        host = meta.get("host", "")
        path = meta.get("path") or ""
        query = meta.get("query") or ""
        method = meta.get("method", "?")

        is_game = "/ep01000/" in path
        endpoint = path.replace("/ep01000/", "") if is_game else path

        files = {}
        for ext in ["req.json", "req.bin", "res.bin", "res.dat", "res.db", "res.json"]:
            fp = os.path.join(extract_dir, f"{idx}-{ext}")
            if os.path.exists(fp):
                files[ext] = fp

        request_path = f"{path.replace('/ep01000', '')}?{query}" if query else path.replace("/ep01000", "")

        requests.append({
            "index": idx,
            "host": host,
            "path": path,
            "query": query,
            "method": method,
            "endpoint": endpoint,
            "request_path": request_path,
            "is_game": is_game,
            "files": files,
            "meta": meta,
        })

    requests.sort(key=lambda r: r["index"])
    return requests


def identify_key(request_path: str, data: bytes, keys: dict[str, bytes]) -> tuple[str, bytes] | None:
    """通过 HMAC 匹配确定加密密钥"""
    for name, key in keys.items():
        if check_hmac(key, request_path, data):
            return name, key
    return None


def analyze_chlz(chlz_path: str, session_key: bytes = None,
                 crypto_log: str = None, account_id: int = None,
                 indices: list[int] = None, export_dir: str = None,
                 verbose: bool = False):
    """分析 chlz 抓包文件"""
    print(f"{'=' * 70}")
    print(f"分析: {os.path.basename(chlz_path)}")
    print(f"{'=' * 70}")

    # 解压
    extract_dir = extract_chlz(chlz_path)
    print(f"解压到: {extract_dir}")

    # 从 crypto log 提取 sessionKey
    if session_key is None and crypto_log:
        session_key = extract_session_key_from_log(crypto_log)

    # 确定账号
    if account_id is None:
        account_id = 2  # 默认账号2
    acct = ACCOUNTS.get(account_id, ACCOUNTS[2])
    login_key = xor_bytes(acct["stored_key"], STARTUP_KEY)

    # 构建密钥候选列表
    keys = {"StartupKey": STARTUP_KEY, "loginKey": login_key}
    if session_key:
        keys["sessionKey"] = session_key

    print(f"账号: {acct['name']} (userId={acct['user_id']})")
    print(f"可用密钥: {', '.join(keys.keys())}")

    # 导出目录
    if export_dir:
        os.makedirs(export_dir, exist_ok=True)

    # 发现请求
    requests = discover_requests(extract_dir)
    print(f"共发现 {len(requests)} 个请求\n")

    # 分析 — 第一遍: 如果有 login/login, 尝试提取 sessionKey
    if session_key is None:
        for req in requests:
            if req["endpoint"] == "login/login" and "res.bin" in req["files"]:
                res_data = open(req["files"]["res.bin"], "rb").read()
                result = identify_key(req["request_path"], res_data, keys)
                if result:
                    _, key = result
                    try:
                        decrypted = decrypt_payload(key, req["request_path"], res_data)
                        r = BytesReader(decrypted)
                        status = r.read_int()
                        auth_count = r.read_int()
                        sk = r.read_bytes()
                        if len(sk) == 32:
                            print(f"从 login/login 响应提取到 resp.SessionKey: {sk.hex()[:16]}...")
                            print(f"  注意: 需要 loginRandom 才能计算 sessionKey")
                            print(f"  提供 --session-key 或 --crypto-log 来解密后续请求\n")
                    except:
                        pass

    # 第二遍: 分析所有请求
    for req in requests:
        if indices and req["index"] not in indices:
            continue

        idx = req["index"]
        tag = "[GAME]" if req["is_game"] else "[EXT] "
        ep = req["endpoint"][:50]

        print(f"--- #{idx} {tag} {req['method']} {ep} ---")

        if not req["is_game"]:
            # 非游戏请求: 显示基本信息
            for ext, fp in req["files"].items():
                size = os.path.getsize(fp)
                ext_label = {
                    "res.dat": "CDN 元数据",
                    "res.db": "数据库文件",
                    "res.json": "JSON 响应",
                    "req.json": "请求体",
                }.get(ext, ext)
                print(f"    {ext}: {size} bytes ({ext_label})")
                if ext == "res.json" and verbose:
                    try:
                        content = json.load(open(fp))
                        print(f"    {json.dumps(content, indent=2, ensure_ascii=False)[:300]}")
                    except:
                        pass
            print()
            continue

        # 游戏请求: 解密
        request_path = req["request_path"]
        print(f"    path: {request_path[:100]}")

        for direction, ext in [("request", "req.json"), ("response", "res.bin")]:
            if ext not in req["files"]:
                continue

            raw = open(req["files"][ext], "rb").read()
            result = identify_key(request_path, raw, keys)

            if result is None:
                print(f"    {direction}: {len(raw)} bytes — 密钥未知 (需要 sessionKey?)")
                continue

            key_name, key = result
            try:
                decrypted = decrypt_payload(key, request_path, raw)
                print(f"    {direction}: {len(decrypted)} bytes (key={key_name})")

                # 导出
                if export_dir:
                    out_path = os.path.join(export_dir, f"{idx}-{direction}.bin")
                    open(out_path, "wb").write(decrypted)

                # 尝试已知解析器
                endpoint_base = req["endpoint"]
                if endpoint_base in KNOWN_PARSERS:
                    try:
                        parsed = KNOWN_PARSERS[endpoint_base](decrypted, direction)
                        for k, v in parsed.items():
                            val_str = str(v)
                            if len(val_str) > 100:
                                val_str = val_str[:100] + "..."
                            print(f"      {k}: {val_str}")
                    except Exception as e:
                        print(f"      解析失败: {e}")
                        print(f"      hex: {decrypted.hex()[:128]}...")
                elif verbose or len(decrypted) <= 200:
                    print(f"      hex: {decrypted.hex()[:200]}{'...' if len(decrypted) > 100 else ''}")
                else:
                    print(f"      hex (前64字节): {decrypted.hex()[:128]}...")

            except Exception as e:
                print(f"    {direction}: 解密失败 ({key_name}): {e}")

        print()

    # 清理
    shutil.rmtree(extract_dir, ignore_errors=True)
    print(f"{'=' * 70}")
    print("分析完成")


# ==============================================================================
# CLI
# ==============================================================================

def main():
    p = argparse.ArgumentParser(description="DQSG chlz 抓包分析工具")
    p.add_argument("chlz", help="chlz 文件路径")
    p.add_argument("--session-key", "-s", help="sessionKey (hex)")
    p.add_argument("--crypto-log", "-c", help="tweak crypto log 文件路径")
    p.add_argument("--account", "-a", type=int, default=2, help="账号ID (1 或 2, 默认 2)")
    p.add_argument("--index", "-i", help="只分析指定序号 (逗号分隔, 如 4,5,7)")
    p.add_argument("--export-dir", "-e", help="导出解密数据到目录")
    p.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    args = p.parse_args()

    session_key = bytes.fromhex(args.session_key) if args.session_key else None
    indices = [int(x) for x in args.index.split(",")] if args.index else None

    analyze_chlz(
        args.chlz,
        session_key=session_key,
        crypto_log=args.crypto_log,
        account_id=args.account,
        indices=indices,
        export_dir=args.export_dir,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()

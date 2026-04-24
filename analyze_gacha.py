#!/usr/bin/env python3
"""
分析 gacha.chlz 抓包文件，解密并解析抽卡相关请求/响应。
"""

import struct
import zlib
import hmac
import hashlib
import os
import json
import zipfile
import tempfile
import re
import shutil
from typing import Optional, List, Dict, Any

# ==============================================================================
# 常量
# ==============================================================================

STARTUP_KEY = bytes.fromhex(
    "5d0c9d1a34076488136525cc60e39581f8a9134374d2983400a50a6879c82498"
)

# Account 4 (from client.py)
ACCT4_STORED_KEY = bytes.fromhex("bd423ddb522964d8799ceacb9b48bced8158ccdcda6d6e36471f2198971ff0c7")
ACCT4_USER_ID = 96317219522

# Account 2
ACCT2_STORED_KEY = bytes.fromhex("761a2fa55bbc43f1c3c6e95e0e14077abb71073348e5841c6f753afba417fd91")

# Account 1
ACCT1_STORED_KEY = bytes.fromhex("7a2216ecb295277fc270209284894853145c1874f0534c9214fa414d89a93cad")


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


# ==============================================================================
# Crypto
# ==============================================================================

def decrypt_payload(key: bytes, request_path: str, data: bytes) -> bytes:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    if len(data) < 36:
        raise ValueError(f"Too short: {len(data)} bytes")
    iv = data[:16]
    mac = data[-20:]
    ct = data[16:-20]
    hmac_input = iv + ct + request_path.encode("utf-8")
    expected_mac = hmac.new(key, hmac_input, hashlib.sha1).digest()
    if mac != expected_mac:
        raise ValueError("HMAC mismatch")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    compressed = unpad(cipher.decrypt(ct), 16)
    if not compressed:
        return b""
    return zlib.decompress(compressed, -15)


def check_hmac(key: bytes, request_path: str, data: bytes) -> bool:
    if len(data) < 36:
        return False
    iv = data[:16]
    mac = data[-20:]
    ct = data[16:-20]
    hmac_input = iv + ct + request_path.encode("utf-8")
    expected = hmac.new(key, hmac_input, hashlib.sha1).digest()
    return mac == expected


# ==============================================================================
# BytesReader
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
        s = self.data[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return s

    def read_bytes(self) -> bytes:
        length = self.read_int()
        b = self.data[self.pos:self.pos + length]
        self.pos += length
        return b

    def read_nullable_string(self):
        if not self.read_bool():
            return None
        return self.read_string()

    def read_nullable_bool(self):
        if not self.read_bool():
            return None
        return self.read_bool()

    def read_nullable_long(self):
        if not self.read_bool():
            return None
        return self.read_long()

    def read_nullable_int(self):
        if not self.read_bool():
            return None
        return self.read_int()

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def peek_hex(self, n=64) -> str:
        end = min(self.pos + n, len(self.data))
        return self.data[self.pos:end].hex()


# ==============================================================================
# Extract sessionKey from console log
# ==============================================================================

def extract_session_key_from_console_log(log_path: str) -> Optional[bytes]:
    """从 iosconsole gacha log 中提取 sessionKey"""
    text = open(log_path).read()
    # Look for XorBytes results after login
    xor_results = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "XorBytes" not in line:
            continue
        if "= (32 bytes) =" in line:
            m = re.search(r'= ([0-9a-f]{64})$', line.strip())
            if m:
                xor_results.append(m.group(1))

    # The session key derivation happens after login callback
    # Pattern: L (storedKey), R (authKey), Result (sessionKey initial)
    # Then: L (loginRandom XOR result), R (resp sessionKey), = sessionKey
    if xor_results:
        # Last XOR result is typically the final session key
        session_key = bytes.fromhex(xor_results[-1])
        print(f"Extracted sessionKey from console log: {session_key.hex()[:16]}...")
        return session_key
    return None


# ==============================================================================
# Parse gacha draw response
# ==============================================================================

def parse_gacha_draw_response(data: bytes) -> Dict[str, Any]:
    """Parse gacha/draw response to get all reward items"""
    r = BytesReader(data)
    status = r.read_int()
    if status != 1:
        return {"status": status, "error": "non-success status"}
    
    reward_count = r.read_int()
    rewards = []
    for i in range(reward_count):
        content_type = r.read_int()
        content_master_id = r.read_int()
        content_amount = r.read_int()
        
        # Read UserWeaponId (nullable long)
        user_weapon_id = r.read_nullable_long()
        # Read UserArmorId (nullable long)  
        user_armor_id = r.read_nullable_long()
        
        reward = {
            "index": i,
            "content_type": content_type,
            "content_master_id": content_master_id,
            "content_master_id_hex": hex(content_master_id),
            "content_amount": content_amount,
            "user_weapon_id": user_weapon_id,
            "user_armor_id": user_armor_id,
        }
        rewards.append(reward)
    
    result = {
        "status": status,
        "reward_count": reward_count,
        "rewards": rewards,
        "remaining_bytes": r.remaining(),
    }
    
    # Try to read remaining data (UserModelDiff etc.)
    if r.remaining() > 0:
        result["remaining_hex_start"] = r.peek_hex(128)
    
    return result


def parse_gacha_draw_request(data: bytes) -> Dict[str, Any]:
    """Parse gacha/draw request"""
    r = BytesReader(data)
    gacha_master_id = r.read_int()
    return {
        "gacha_master_id": gacha_master_id,
        "gacha_master_id_hex": hex(gacha_master_id),
    }


def parse_gacha_fetch_list_response(data: bytes) -> Dict[str, Any]:
    """Parse gacha/fetch_list response"""
    r = BytesReader(data)
    status = r.read_int()
    # Try to read remaining as int32 list (gacha IDs)
    result = {"status": status}
    if r.remaining() >= 4:
        remaining_data = data[r.pos:]
        result["remaining_hex"] = remaining_data.hex()
        result["remaining_size"] = len(remaining_data)
        # Try reading as count + items
        try:
            count = r.read_int()
            result["count"] = count
            items = []
            for _ in range(count):
                items.append(r.read_int())
            result["gacha_ids"] = items
            result["gacha_ids_hex"] = [hex(x) for x in items]
        except:
            pass
    return result


def parse_login_response_full(data: bytes) -> Dict[str, Any]:
    """Parse login/login response including UserModel armors"""
    r = BytesReader(data)
    
    status = r.read_int()
    auth_count = r.read_int()
    session_key = r.read_bytes()
    client_id = r.read_string()
    in_game_session_id = r.read_nullable_long()
    perf_metrics = r.read_bool()
    asset_cdn_url = r.read_string()
    
    result = {
        "status": status,
        "auth_count": auth_count,
        "session_key_len": len(session_key),
        "client_id": client_id,
        "in_game_session_id": in_game_session_id,
    }
    
    # Check if there's a UserModel
    if r.remaining() < 1:
        result["has_user_model"] = False
        return result
    
    has_user_model = r.read_bool()
    result["has_user_model"] = has_user_model
    
    if not has_user_model:
        return result
    
    # Parse UserModel lists
    # List 1: UserWeapon (long, int, bool, long)
    try:
        count = r.read_int()
        weapons = []
        for _ in range(count):
            weapon = {
                "UserWeaponId": r.read_long(),
                "WeaponMasterId": r.read_int(),
                "IsLock": r.read_bool(),
                "AcquiredAt": r.read_long(),
            }
            weapons.append(weapon)
        result["weapons"] = weapons
        result["weapon_count"] = count
        
        # List 2: int list (might be weapon master IDs or something)
        count2 = r.read_int()
        list2 = [r.read_int() for _ in range(count2)]
        result["list2_count"] = count2
        
        # List 3: (int, int) pairs
        count3 = r.read_int()
        list3 = [(r.read_int(), r.read_int()) for _ in range(count3)]
        result["list3_count"] = count3
        
        # List 4: long list (might be IDs to delete)
        count4 = r.read_int()
        list4 = [r.read_long() for _ in range(count4)]
        result["list4_count"] = count4
        
        # List 5: (int, int) pairs
        count5 = r.read_int()
        list5 = [(r.read_int(), r.read_int()) for _ in range(count5)]
        result["list5_count"] = count5
        
        # List 6: int list
        count6 = r.read_int()
        list6 = [r.read_int() for _ in range(count6)]
        result["list6_count"] = count6
        
        # List 7: UserArmor
        count7 = r.read_int()
        armors = []
        for _ in range(count7):
            armor = {
                "UserArmorId": r.read_long(),
                "ArmorMasterId": r.read_int(),
                "Level": r.read_int(),
                "LevelExp": r.read_int(),
                "LimitBreakStep": r.read_int(),
                "IsLock": r.read_bool(),
                "AcquiredAt": r.read_long(),
            }
            armors.append(armor)
        result["armors"] = armors
        result["armor_count"] = count7
        
    except Exception as e:
        result["parse_error"] = str(e)
        result["parse_pos"] = r.pos
    
    return result


# ==============================================================================
# Main analysis
# ==============================================================================

def main():
    import argparse
    p = argparse.ArgumentParser(description="分析 gacha.chlz")
    p.add_argument("chlz", help="chlz file path")
    p.add_argument("--console-log", "-c", help="iOS console log file")
    p.add_argument("--session-key", "-s", help="sessionKey hex")
    p.add_argument("--export-dir", "-e", help="Export decrypted binaries")
    args = p.parse_args()
    
    # Determine session key
    session_key = None
    if args.session_key:
        session_key = bytes.fromhex(args.session_key)
    elif args.console_log:
        session_key = extract_session_key_from_console_log(args.console_log)
    
    # Try all known accounts' login keys
    login_keys = {}
    for name, stored_key in [("ACCT1", ACCT1_STORED_KEY), ("ACCT2", ACCT2_STORED_KEY), ("ACCT4", ACCT4_STORED_KEY)]:
        login_keys[f"loginKey_{name}"] = xor_bytes(stored_key, STARTUP_KEY)
    
    keys = {"StartupKey": STARTUP_KEY}
    keys.update(login_keys)
    if session_key:
        keys["sessionKey"] = session_key
    
    print(f"Available keys: {', '.join(keys.keys())}")
    
    # Extract chlz
    extract_dir = tempfile.mkdtemp(prefix="gacha_")
    with zipfile.ZipFile(args.chlz, 'r') as z:
        z.extractall(extract_dir)
    print(f"Extracted to: {extract_dir}")
    
    if args.export_dir:
        os.makedirs(args.export_dir, exist_ok=True)
    
    # Discover requests
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
            "endpoint": endpoint,
            "request_path": request_path,
            "is_game": is_game,
            "files": files,
        })
    
    requests.sort(key=lambda r: r["index"])
    
    # First pass - try to extract session key from login response
    if session_key is None:
        for req in requests:
            if req["endpoint"] == "login/login" and "res.bin" in req["files"]:
                res_data = open(req["files"]["res.bin"], "rb").read()
                for key_name, key in keys.items():
                    if check_hmac(key, req["request_path"], res_data):
                        try:
                            decrypted = decrypt_payload(key, req["request_path"], res_data)
                            r = BytesReader(decrypted)
                            r.read_int()  # status
                            r.read_int()  # auth_count
                            sk = r.read_bytes()
                            if len(sk) == 32:
                                print(f"\nFound resp.SessionKey in login/login (key={key_name}): {sk.hex()[:16]}...")
                                print(f"NOTE: Need loginRandom to compute actual sessionKey")
                        except:
                            pass
    
    # If we have the console log, extract sessionKey from XorBytes
    # From the console log, the session key should be directly obtainable
    # Line 11: = (32 bytes) = 9647a9f480c4054e36854fda692dccee3d9ed177ba6183191f0a150418f515c9
    # But that's for the first login phase. The actual session key after login/login #12 is derived differently.
    # Line 25: = (32 bytes) = 57d8e851f96ad8601aff24b0c3dfbcf75c2807070587952ea9aa970e97880acb
    # This is the actual session key used for all subsequent requests
    
    # Let's try all XOR results from the log as potential session keys
    if args.console_log and session_key is None:
        text = open(args.console_log).read()
        xor_results = []
        for line in text.split("\n"):
            m = re.search(r'= \(32 bytes\) = ([0-9a-f]{64})', line)
            if m:
                xor_results.append(bytes.fromhex(m.group(1)))
        
        # Try each as session key
        for i, candidate in enumerate(xor_results):
            candidate_name = f"xor_result_{i}"
            # Test against a known game request
            for req in requests:
                if req["endpoint"] in ("gacha/draw", "gacha/fetch_list", "gacha/fetch_top"):
                    for ext in ["res.bin", "req.json"]:
                        if ext in req["files"]:
                            raw = open(req["files"][ext], "rb").read()
                            if check_hmac(candidate, req["request_path"], raw):
                                session_key = candidate
                                keys["sessionKey"] = session_key
                                print(f"\nFound sessionKey via HMAC match (xor_result_{i}): {session_key.hex()[:16]}...")
                                break
                    if session_key:
                        break
            if session_key:
                break
    
    print(f"\n{'='*70}")
    print(f"Analyzing {len(requests)} requests from gacha.chlz")
    print(f"{'='*70}\n")
    
    gacha_draw_count = 0
    
    for req in requests:
        if not req["is_game"]:
            continue
        
        idx = req["index"]
        ep = req["endpoint"]
        
        # Focus on gacha-related and login endpoints
        is_interesting = any(keyword in ep for keyword in ["gacha", "login/login"])
        
        if not is_interesting:
            # Print brief info for other endpoints
            print(f"--- #{idx} {ep} ---")
            for direction, ext in [("request", "req.json"), ("response", "res.bin")]:
                if ext not in req["files"]:
                    continue
                raw = open(req["files"][ext], "rb").read()
                for key_name, key in keys.items():
                    if check_hmac(key, req["request_path"], raw):
                        try:
                            decrypted = decrypt_payload(key, req["request_path"], raw)
                            print(f"    {direction}: {len(decrypted)} bytes (key={key_name})")
                            if args.export_dir:
                                out = os.path.join(args.export_dir, f"{idx}-{direction}.bin")
                                open(out, "wb").write(decrypted)
                        except:
                            print(f"    {direction}: decrypt failed")
                        break
                else:
                    print(f"    {direction}: {len(raw)} bytes — key unknown")
            print()
            continue
        
        print(f"\n{'='*50}")
        print(f"  #{idx} {ep}")
        print(f"{'='*50}")
        
        for direction, ext in [("request", "req.json"), ("response", "res.bin")]:
            if ext not in req["files"]:
                continue
            raw = open(req["files"][ext], "rb").read()
            
            matched_key = None
            matched_key_name = None
            for key_name, key in keys.items():
                if check_hmac(key, req["request_path"], raw):
                    matched_key = key
                    matched_key_name = key_name
                    break
            
            if matched_key is None:
                print(f"    {direction}: {len(raw)} bytes — KEY UNKNOWN")
                continue
            
            try:
                decrypted = decrypt_payload(matched_key, req["request_path"], raw)
            except Exception as e:
                print(f"    {direction}: decrypt failed ({matched_key_name}): {e}")
                continue
            
            print(f"\n  [{direction}] {len(decrypted)} bytes (key={matched_key_name})")
            print(f"    hex: {decrypted.hex()[:200]}...")
            
            if args.export_dir:
                out = os.path.join(args.export_dir, f"{idx}-{direction}.bin")
                open(out, "wb").write(decrypted)
            
            # Parse known formats
            if ep == "gacha/draw":
                if direction == "request":
                    parsed = parse_gacha_draw_request(decrypted)
                    print(f"    gacha_master_id: {parsed['gacha_master_id']} ({parsed['gacha_master_id_hex']})")
                elif direction == "response":
                    gacha_draw_count += 1
                    parsed = parse_gacha_draw_response(decrypted)
                    print(f"    status: {parsed['status']}")
                    print(f"    reward_count: {parsed['reward_count']}")
                    print(f"\n    === GACHA DRAW #{gacha_draw_count} RESULTS ===")
                    for rw in parsed.get("rewards", []):
                        armor_or_weapon = "WEAPON" if rw["user_weapon_id"] else ("ARMOR" if rw["user_armor_id"] else "OTHER")
                        uid = rw["user_weapon_id"] or rw["user_armor_id"] or "-"
                        print(f"      [{rw['index']}] type={rw['content_type']}, master_id={rw['content_master_id']} ({rw['content_master_id_hex']}), "
                              f"amount={rw['content_amount']}, {armor_or_weapon} uid={uid}")
                    print(f"    remaining_bytes: {parsed.get('remaining_bytes', '?')}")
            
            elif ep == "gacha/fetch_list":
                if direction == "response":
                    parsed = parse_gacha_fetch_list_response(decrypted)
                    print(f"    status: {parsed['status']}")
                    if "gacha_ids" in parsed:
                        print(f"    gacha_ids: {parsed['gacha_ids']}")
                        print(f"    gacha_ids_hex: {parsed['gacha_ids_hex']}")
            
            elif ep == "gacha/fetch_top":
                if direction == "response":
                    r = BytesReader(decrypted)
                    print(f"    status: {r.read_int()}")
                    if r.remaining() >= 4:
                        remaining = decrypted[r.pos:]
                        print(f"    remaining hex: {remaining.hex()}")
            
            elif ep == "login/login":
                if direction == "response":
                    parsed = parse_login_response_full(decrypted)
                    print(f"    status: {parsed['status']}")
                    print(f"    auth_count: {parsed['auth_count']}")
                    print(f"    has_user_model: {parsed.get('has_user_model')}")
                    if "weapons" in parsed:
                        print(f"\n    === WEAPONS ({parsed['weapon_count']}) ===")
                        for w in parsed["weapons"]:
                            print(f"      WeaponMasterId={w['WeaponMasterId']} ({hex(w['WeaponMasterId'])}), "
                                  f"UserWeaponId={w['UserWeaponId']}, IsLock={w['IsLock']}")
                    if "armors" in parsed:
                        print(f"\n    === ARMORS ({parsed['armor_count']}) ===")
                        for a in parsed["armors"]:
                            print(f"      ArmorMasterId={a['ArmorMasterId']} ({hex(a['ArmorMasterId'])}), "
                                  f"UserArmorId={a['UserArmorId']}, Level={a['Level']}, "
                                  f"LimitBreak={a['LimitBreakStep']}, IsLock={a['IsLock']}")
                    if "parse_error" in parsed:
                        print(f"    PARSE ERROR: {parsed['parse_error']} at pos {parsed.get('parse_pos')}")
    
    # Cleanup
    shutil.rmtree(extract_dir, ignore_errors=True)
    print(f"\n{'='*70}")
    print("Analysis complete")


if __name__ == "__main__":
    main()

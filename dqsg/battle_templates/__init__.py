"""Battle result templates.

Each template is a captured in_game/result body for a specific stage.
Dynamic fields patched at replay time:
  - offset 0: stage_master_id (int32) — patched when using a different stage
  - offset 4: in_game_session_id (int64) — patched from in_game/start when available
  - reward prefix — patched from the current in_game/start FixedStageLot when available

Template files:
  stage_10101101.bin  — Chapter 1-1 (tutorial stage)
  stage_10101102.bin  — Chapter 1-2 (captured from 1-2to1-3 flow)
  stage_10101103.bin  — Chapter 1-3 (captured from 1-2to1-3 flow)
  stage_10101201.bin  — Chapter 1 hard 1-1
  stage_10102101.bin  — Chapter 2 default story result
  stage_10131101.bin  — Slime growth dungeon Lv.1
  stage_10132211.bin  — Gold growth dungeon Lv.1
  stage_30144101.bin  — Strong enemy clash Lv.1
  stage_10151701_hd_jx.bin  — Event colossus
"""

import json
import os
import struct
import time

from ..serialization import BytesReader, BytesWriter

_TEMPLATE_DIR = os.path.dirname(__file__)

# Field offsets in the binary header
_STAGE_ID_OFFSET = 0
_SESSION_ID_OFFSET = 4

_CONTENT_TYPE_GOLD = 2
_CONTENT_TYPE_ORB_RANK = 131
_CONTENT_TYPE_STYLE_EXP = 5000


def battle_template_exists(stage_master_id: int, template_file: str = None) -> bool:
    filename = template_file or f"stage_{stage_master_id}.bin"
    path = os.path.join(_TEMPLATE_DIR, filename)
    return os.path.exists(path)


def load_battle_result(stage_master_id: int,
                       template_stage_id: int = None,
                       in_game_session_id: int = None,
                       template_file: str = None,
                       start_response: dict = None,
                       dynamic_rewards: bool = True,
                       damage_taken: int = None,
                       damage_taken_count: int = None,
                       dead_count: int = None,
                       clear_time: int = None) -> bytes:
    """Load a battle result template and patch dynamic fields.

    Args:
        stage_master_id: The stage to report victory for.
        template_stage_id: Which template file to use. If None, uses
            stage_master_id (i.e. expects a template for that exact stage).
            Set this to reuse another stage's template (e.g. 10101101 for 1-1).

    Returns the complete in_game/result request body ready to send.
    Raises FileNotFoundError if no template exists.
    """
    src = template_stage_id or stage_master_id
    filename = template_file or f"stage_{src}.bin"
    path = os.path.join(_TEMPLATE_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No battle template for stage {src}. "
            f"Capture one and save to {path}"
        )

    body = bytearray(open(path, "rb").read())

    # Patch stage_master_id
    struct.pack_into('<i', body, _STAGE_ID_OFFSET, stage_master_id)

    # Captured templates also embed stage-specific block ids as decimal strings
    # inside the KPI JSON payload (for example 1010110102). When reusing a
    # template for another stage in the same chapter, rewrite the stage prefix
    # so those block ids stay aligned with the requested stage.
    if src != stage_master_id:
        src_stage_ascii = str(src).encode("ascii")
        dst_stage_ascii = str(stage_master_id).encode("ascii")
        if len(src_stage_ascii) != len(dst_stage_ascii):
            raise ValueError(
                f"Cannot rewrite template stage {src} to {stage_master_id}: "
                "stage id widths differ"
            )
        body = bytearray(bytes(body).replace(src_stage_ascii, dst_stage_ascii))

    # Patch in_game_session_id. When resuming an existing battle, reuse the
    # server-issued session id; otherwise generate a fresh nanosecond timestamp.
    session_id = _resolve_session_id(in_game_session_id, start_response)
    struct.pack_into('<q', body, _SESSION_ID_OFFSET, session_id)

    if dynamic_rewards and start_response:
        body = _patch_dynamic_rewards(body, stage_master_id, session_id, start_response)

    _patch_result_stats(
        body,
        damage_taken=damage_taken,
        damage_taken_count=damage_taken_count,
        dead_count=dead_count,
        clear_time=clear_time,
    )

    return bytes(body)


# ===========================================================================
# Juxiang (巨像) specific template loader
# ===========================================================================

# stage_10151701.bin layout specific offsets:
#   0:  stage_master_id (int32)
#   4:  in_game_session_id (int64)
#  12:  20 bytes of zeros (flags/padding)
#  32:  entity_count (int32) = 7
#  36:  7 × entity_master_id (int32)
#  64:  victory_flag (int32) = 1
#  68:  has_score (bool, 1 byte) = 1
#  69:  score (int32) — the total damage score
#  73:  received_damage_total (int32)
#  77+: remaining stats...

_JUXIANG_STAGE_ID = 10151701
_JUXIANG_SCORE_OFFSET = 69     # score int32 starts after the 1-byte bool at 68


def load_juxiang_result(score: int,
                        in_game_session_id: int = None,
                        damage_taken: int = None,
                        damage_taken_count: int = None,
                        dead_count: int = None,
                        clear_time: int = None) -> bytes:
    """Load 巨像 battle result template and patch score + session id.

    Args:
        score: The damage score to report.
        in_game_session_id: Session id from in_game/start response.
            If None, generates a fresh nanosecond timestamp.

    Returns the patched in_game/result body.
    """
    path = os.path.join(_TEMPLATE_DIR, f"stage_{_JUXIANG_STAGE_ID}.bin")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing juxiang template: {path}")

    body = bytearray(open(path, "rb").read())

    # Keep stage_master_id as-is (already correct in the template)

    # Patch session id
    session_id = in_game_session_id if in_game_session_id is not None else time.time_ns()
    struct.pack_into('<q', body, _SESSION_ID_OFFSET, session_id)

    # Patch score
    struct.pack_into('<i', body, _JUXIANG_SCORE_OFFSET, score)

    _patch_result_stats(
        body,
        damage_taken=damage_taken,
        damage_taken_count=damage_taken_count,
        dead_count=dead_count,
        clear_time=clear_time,
    )

    return bytes(body)


# ===========================================================================
# Generic scored-dungeon template loader
# ===========================================================================

# Score offset is at: 36 + entity_count*4 + 4 (victory_flag) + 1 (has_score bool)
# = 41 + entity_count * 4
_ENTITY_COUNT_OFFSET = 32


def load_scored_result(stage_master_id: int,
                       score: int,
                       template_stage_id: int = None,
                       in_game_session_id: int = None,
                       score_mirror_offsets: list[int] = None,
                       template_file: str = None,
                       start_response: dict = None,
                       dynamic_rewards: bool = True,
                       damage_taken: int = None,
                       damage_taken_count: int = None,
                       dead_count: int = None,
                       clear_time: int = None) -> bytes:
    """Load a scored dungeon template, patch session id and score.

    Works for any dungeon that uses the standard result layout:
      +0:  stage_master_id (int32)
      +4:  in_game_session_id (int64)
      +32: entity_count (int32)
      +36: entity_master_ids (entity_count × int32)
      +N:  victory_flag (int32)
      +N+4: has_score (bool, 1 byte)
      +N+5: score (int32)   ← patched

    Args:
        stage_master_id: Stage to report victory for.
        score: The score to inject.
        template_stage_id: Which template file to use. If None, uses
            stage_master_id. Set this to reuse another stage's template.
        in_game_session_id: If None, generates a fresh nanosecond timestamp.
        score_mirror_offsets: Optional extra int32 offsets that mirror score.
        template_file: Optional explicit template filename in this directory.
    """
    src = template_stage_id or stage_master_id
    filename = template_file or f"stage_{src}.bin"
    path = os.path.join(_TEMPLATE_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No battle template for stage {src}. "
            f"Capture one and save to {path}"
        )

    body = bytearray(open(path, "rb").read())

    # Patch stage_master_id
    struct.pack_into('<i', body, _STAGE_ID_OFFSET, stage_master_id)

    # Captured scored templates can also embed stage-specific block ids in
    # KPI JSON, for example 301441011. Keep those aligned when reusing a
    # lower-level template for a higher difficulty of the same event.
    if src != stage_master_id:
        src_stage_ascii = str(src).encode("ascii")
        dst_stage_ascii = str(stage_master_id).encode("ascii")
        if len(src_stage_ascii) != len(dst_stage_ascii):
            raise ValueError(
                f"Cannot rewrite template stage {src} to {stage_master_id}: "
                "stage id widths differ"
            )
        body = bytearray(bytes(body).replace(src_stage_ascii, dst_stage_ascii))

    # Patch session id
    session_id = _resolve_session_id(in_game_session_id, start_response)
    struct.pack_into('<q', body, _SESSION_ID_OFFSET, session_id)

    # Find score offset dynamically by scanning for victory_flag(1) + has_score(true)
    # pattern. The header between session_id and entity list varies by dungeon type.
    score_offset = _find_score_offset(body)
    struct.pack_into('<i', body, score_offset, score)
    for offset in score_mirror_offsets or []:
        struct.pack_into('<i', body, offset, score)

    if dynamic_rewards and start_response:
        body = _patch_dynamic_rewards(body, stage_master_id, session_id, start_response)
        for offset in score_mirror_offsets or []:
            if offset < _result_reward_prefix_end(body):
                struct.pack_into('<i', body, offset, score)

    _patch_result_stats(
        body,
        damage_taken=damage_taken,
        damage_taken_count=damage_taken_count,
        dead_count=dead_count,
        clear_time=clear_time,
    )

    return bytes(body)


def _resolve_session_id(in_game_session_id: int = None,
                        start_response: dict = None) -> int:
    if in_game_session_id is not None:
        return in_game_session_id
    if start_response and start_response.get("SessionId") is not None:
        return start_response["SessionId"]
    return time.time_ns()


def _patch_result_stats(body: bytearray,
                        *,
                        damage_taken: int = None,
                        damage_taken_count: int = None,
                        dead_count: int = None,
                        clear_time: int = None):
    if (
        damage_taken is None
        and damage_taken_count is None
        and dead_count is None
        and clear_time is None
    ):
        return

    offsets = _result_stat_offsets(body)
    if damage_taken is not None:
        struct.pack_into("<i", body, offsets["DamageTaken"], damage_taken)
    if damage_taken_count is not None:
        struct.pack_into("<i", body, offsets["DamageTakenCount"], damage_taken_count)
    if dead_count is not None:
        struct.pack_into("<i", body, offsets["DeadCount"], dead_count)
    if clear_time is not None:
        struct.pack_into("<i", body, offsets["ClearTime"], clear_time)


def _result_stat_offsets(body: bytearray) -> dict[str, int]:
    r = BytesReader(body)
    r.read_int()   # StageMasterId
    r.read_long()  # SessionId
    r.read_int()   # Gold
    r.read_int()   # StyleExp

    drop_content_count = r.read_int()
    if drop_content_count < 0 or drop_content_count > 1000:
        raise ValueError(f"Invalid DropContentList count: {drop_content_count}")
    r.pos += drop_content_count * 12

    drop_orb_count = r.read_int()
    if drop_orb_count < 0 or drop_orb_count > 1000:
        raise ValueError(f"Invalid DropContentOrbList count: {drop_orb_count}")
    r.pos += drop_orb_count * 12

    treasure_box_count = r.read_int()
    if treasure_box_count < 0 or treasure_box_count > 10000:
        raise ValueError(f"Invalid TreasureBoxIdList count: {treasure_box_count}")
    r.pos += treasure_box_count * 4

    enemy_count = r.read_int()
    if enemy_count < 0 or enemy_count > 10000:
        raise ValueError(
            f"Invalid EncounteredEnemyUnitMasterIdList count: {enemy_count}"
        )
    r.pos += enemy_count * 4

    r.read_int()  # TransitionType
    if r.read_bool():
        r.read_int()  # Score

    return {
        "DamageTaken": r.pos,
        "DamageTakenCount": r.pos + 4,
        "DeadCount": r.pos + 8,
        "ClearTime": r.pos + 12,
    }


def _result_reward_prefix_end(body: bytearray) -> int:
    r = BytesReader(body)
    r.read_int()   # StageMasterId
    r.read_long()  # SessionId
    r.read_int()   # Gold
    r.read_int()   # StyleExp

    content_count = r.read_int()
    if content_count < 0 or content_count > 1000:
        raise ValueError(f"Invalid DropContentList count: {content_count}")
    r.pos += content_count * 12

    orb_count = r.read_int()
    if orb_count < 0 or orb_count > 1000:
        raise ValueError(f"Invalid DropContentOrbList count: {orb_count}")
    r.pos += orb_count * 12
    return r.pos


def _read_result_reward_prefix(body: bytearray) -> dict:
    r = BytesReader(body)
    r.read_int()   # StageMasterId
    r.read_long()  # SessionId
    result = {
        "Gold": r.read_int(),
        "StyleExp": r.read_int(),
        "DropContentList": [],
        "DropContentOrbList": [],
    }

    content_count = r.read_int()
    if content_count < 0 or content_count > 1000:
        raise ValueError(f"Invalid DropContentList count: {content_count}")
    for _ in range(content_count):
        result["DropContentList"].append({
            "ContentType": r.read_int(),
            "ContentMasterId": r.read_int(),
            "ContentAmount": r.read_int(),
        })

    orb_count = r.read_int()
    if orb_count < 0 or orb_count > 1000:
        raise ValueError(f"Invalid DropContentOrbList count: {orb_count}")
    for _ in range(orb_count):
        result["DropContentOrbList"].append({
            "OrbMasterId": r.read_int(),
            "OrbRank": r.read_int(),
            "Amount": r.read_int(),
        })
    return result


def _template_cleared_block_ids(body: bytearray) -> set[int]:
    marker = b'{"kpi"'
    start = bytes(body).find(marker)
    if start < 4:
        return set()
    length = struct.unpack_from("<i", body, start - 4)[0]
    if length <= 0 or start + length > len(body):
        return set()
    try:
        payload = json.loads(bytes(body[start:start + length]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return set()
    blocks = (
        payload.get("kpi", {})
        .get("cleared_wave", {})
        .get("block", [])
    )
    return {int(block) for block in blocks}


def _write_result_reward_prefix(stage_master_id: int,
                                session_id: int,
                                rewards: dict) -> bytes:
    w = BytesWriter()
    w.write_int(stage_master_id)
    w.write_long(session_id)
    w.write_int(rewards["Gold"])
    w.write_int(rewards["StyleExp"])

    contents = rewards["DropContentList"]
    w.write_int(len(contents))
    for content in contents:
        w.write_int(content["ContentType"])
        w.write_int(content["ContentMasterId"])
        w.write_int(content["ContentAmount"])

    orbs = rewards["DropContentOrbList"]
    w.write_int(len(orbs))
    for orb in orbs:
        w.write_int(orb["OrbMasterId"])
        w.write_int(orb["OrbRank"])
        w.write_int(orb["Amount"])

    return w.to_bytes()


def _patch_dynamic_rewards(body: bytearray,
                           stage_master_id: int,
                           session_id: int,
                           start_response: dict) -> bytearray:
    rewards = _build_dynamic_rewards(body, start_response)
    prefix = _write_result_reward_prefix(stage_master_id, session_id, rewards)
    suffix = body[_result_reward_prefix_end(body):]
    return bytearray(prefix + suffix)


def _build_dynamic_rewards(body: bytearray, start_response: dict) -> dict:
    fixed_stage_lot = start_response.get("FixedStageLot") or {}
    template_rewards = _read_result_reward_prefix(body)
    cleared_blocks = _template_cleared_block_ids(body)

    rewards = {
        "Gold": 0,
        "StyleExp": 0,
        "DropContentList": [],
        "DropContentOrbList": [],
    }

    block_rewards = fixed_stage_lot.get("BlockClearRewardList") or []
    selected_block_rewards = [
        reward for reward in block_rewards
        if _include_block(reward.get("BlockMasterId"), cleared_blocks)
    ]
    if cleared_blocks and block_rewards and not selected_block_rewards:
        selected_block_rewards = block_rewards

    for reward in selected_block_rewards:
        for content in reward.get("DropContentList") or []:
            _add_dynamic_reward(rewards, content)

    for treasure in fixed_stage_lot.get("GimmickTreasureBoxList") or []:
        content = treasure.get("DropContent") or {}
        if content.get("ContentType") != _CONTENT_TYPE_GOLD:
            _add_dynamic_reward(rewards, content)

    for mimic in fixed_stage_lot.get("GimmickEnemySpawnerMimicList") or []:
        block_id = _block_id_from_gimmick_id(mimic.get("GimmickPlacementId"))
        if not _include_block(block_id, cleared_blocks):
            continue
        for content in mimic.get("DropContentList") or []:
            _add_dynamic_reward(rewards, content)

    for roulette in fixed_stage_lot.get("GimmickRouletteList") or []:
        block_id = _block_id_from_gimmick_id(roulette.get("GimmickPlacementId"))
        if _include_block(block_id, cleared_blocks):
            _add_dynamic_reward(rewards, roulette.get("DropContent") or {})

    for enemy_drop in fixed_stage_lot.get("EnemyDropList") or []:
        block_id = enemy_drop.get("BlockId")
        if block_id is None:
            block_id = _block_id_from_team_id(enemy_drop.get("TeamId"))
        if not _include_block(block_id, cleared_blocks):
            continue
        for content in enemy_drop.get("DropContentList") or []:
            _add_dynamic_reward(rewards, content)

    shaped = _shape_dynamic_rewards(template_rewards, rewards)
    _add_template_shaped_treasure_gold(shaped, template_rewards, rewards, fixed_stage_lot)
    return shaped


def _shape_dynamic_rewards(template_rewards: dict, dynamic_rewards: dict) -> dict:
    return {
        "Gold": dynamic_rewards["Gold"],
        "StyleExp": dynamic_rewards["StyleExp"],
        "DropContentList": _shape_dynamic_contents(
            template_rewards["DropContentList"],
            dynamic_rewards["DropContentList"],
        ),
        "DropContentOrbList": _shape_dynamic_orbs(
            template_rewards["DropContentOrbList"],
            dynamic_rewards["DropContentOrbList"],
        ),
    }


def _shape_dynamic_contents(template_contents: list[dict],
                            dynamic_contents: list[dict]) -> list[dict]:
    remaining = list(dynamic_contents)
    selected = []
    for template_content in template_contents:
        idx = _find_dynamic_content_index(remaining, template_content, exact_master=True)
        if idx is None:
            idx = _find_dynamic_content_index(remaining, template_content, exact_master=False)
        if idx is None:
            continue
        selected.append(remaining.pop(idx))
    return selected


def _find_dynamic_content_index(contents: list[dict],
                                template_content: dict,
                                *,
                                exact_master: bool) -> int | None:
    for idx, content in enumerate(contents):
        if content.get("ContentType") != template_content.get("ContentType"):
            continue
        if exact_master and content.get("ContentMasterId") != template_content.get("ContentMasterId"):
            continue
        return idx
    return None


def _shape_dynamic_orbs(template_orbs: list[dict],
                        dynamic_orbs: list[dict]) -> list[dict]:
    remaining = list(dynamic_orbs)
    selected = []
    for template_orb in template_orbs:
        idx = _find_dynamic_orb_index(remaining, template_orb, exact_master=True)
        if idx is None:
            idx = _find_dynamic_orb_index(remaining, template_orb, exact_master=False)
        if idx is None:
            continue
        selected.append(remaining.pop(idx))
    return selected


def _find_dynamic_orb_index(orbs: list[dict],
                            template_orb: dict,
                            *,
                            exact_master: bool) -> int | None:
    for idx, orb in enumerate(orbs):
        if exact_master and (
            orb.get("OrbMasterId") != template_orb.get("OrbMasterId")
            or orb.get("OrbRank") != template_orb.get("OrbRank")
        ):
            continue
        return idx
    return None


def _add_template_shaped_treasure_gold(shaped_rewards: dict,
                                       template_rewards: dict,
                                       block_rewards: dict,
                                       fixed_stage_lot: dict):
    missing_gold = template_rewards["Gold"] - block_rewards["Gold"]
    if missing_gold <= 0:
        return
    for treasure in fixed_stage_lot.get("GimmickTreasureBoxList") or []:
        content = treasure.get("DropContent") or {}
        if content.get("ContentType") != _CONTENT_TYPE_GOLD:
            continue
        amount = content.get("ContentAmount") or 0
        if amount <= 0:
            continue
        shaped_rewards["Gold"] += amount
        missing_gold -= amount
        if missing_gold <= 0:
            break


def _include_block(block_id, cleared_blocks: set[int]) -> bool:
    if not cleared_blocks:
        return True
    return block_id in cleared_blocks


def _block_id_from_gimmick_id(gimmick_id):
    if gimmick_id is None:
        return None
    return int(gimmick_id) // 100000


def _block_id_from_team_id(team_id):
    if team_id is None:
        return None
    return int(team_id) // 1000


def _add_dynamic_reward(rewards: dict, content: dict):
    content_type = content.get("ContentType")
    master_id = content.get("ContentMasterId")
    amount = content.get("ContentAmount")
    if content_type is None or amount is None:
        return

    if content_type == _CONTENT_TYPE_GOLD:
        rewards["Gold"] += amount
        return
    if content_type == _CONTENT_TYPE_STYLE_EXP:
        rewards["StyleExp"] += amount
        return
    if content_type == _CONTENT_TYPE_ORB_RANK:
        orb_master_id, orb_rank = _decode_orb_rank_master_id(master_id)
        rewards["DropContentOrbList"].append({
            "OrbMasterId": orb_master_id,
            "OrbRank": orb_rank,
            "Amount": amount,
        })
        return

    rewards["DropContentList"].append({
        "ContentType": content_type,
        "ContentMasterId": master_id or 0,
        "ContentAmount": amount,
    })


def _decode_orb_rank_master_id(master_id: int) -> tuple[int, int]:
    if master_id is None:
        return 0, 0
    return (master_id // 100) % 1000000, master_id % 100


def read_template_score(stage_master_id: int,
                        template_stage_id: int = None,
                        template_file: str = None) -> int:
    """Read the original score value stored in a scored template."""
    src = template_stage_id or stage_master_id
    filename = template_file or f"stage_{src}.bin"
    path = os.path.join(_TEMPLATE_DIR, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No battle template for stage {src}. "
            f"Capture one and save to {path}"
        )

    body = bytearray(open(path, "rb").read())
    score_offset = _find_score_offset(body)
    return struct.unpack_from('<i', body, score_offset)[0]


def _find_score_offset(body: bytearray) -> int:
    """Find score int32 offset by locating victory_flag=1 + has_score=1 pattern."""
    for pos in range(32, min(200, len(body) - 5)):
        victory = struct.unpack_from('<i', body, pos)[0]
        has_score = body[pos + 4]
        if victory != 1 or has_score != 1:
            continue
        # Validate: walk back to find entity_count that aligns
        for ec_pos in range(pos - 4, 28, -4):
            ec = struct.unpack_from('<i', body, ec_pos)[0]
            if 1 <= ec <= 20 and (pos - (ec_pos + 4)) == ec * 4:
                return pos + 5
    raise ValueError("Cannot find score offset in template")

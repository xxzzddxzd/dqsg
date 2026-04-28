"""Battle result templates.

Each template is a captured in_game/result body for a specific stage.
Dynamic fields patched at replay time:
  - offset 0: stage_master_id (int32) — patched when using a different stage
  - offset 4: in_game_session_id (int64) — always patched with time.time_ns()

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

import os
import struct
import time

_TEMPLATE_DIR = os.path.dirname(__file__)

# Field offsets in the binary header
_STAGE_ID_OFFSET = 0
_SESSION_ID_OFFSET = 4


def battle_template_exists(stage_master_id: int, template_file: str = None) -> bool:
    filename = template_file or f"stage_{stage_master_id}.bin"
    path = os.path.join(_TEMPLATE_DIR, filename)
    return os.path.exists(path)


def load_battle_result(stage_master_id: int,
                       template_stage_id: int = None,
                       in_game_session_id: int = None,
                       template_file: str = None) -> bytes:
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
    session_id = in_game_session_id if in_game_session_id is not None else time.time_ns()
    struct.pack_into('<q', body, _SESSION_ID_OFFSET, session_id)

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
                        in_game_session_id: int = None) -> bytes:
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
                       template_file: str = None) -> bytes:
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
    session_id = in_game_session_id if in_game_session_id is not None else time.time_ns()
    struct.pack_into('<q', body, _SESSION_ID_OFFSET, session_id)

    # Find score offset dynamically by scanning for victory_flag(1) + has_score(true)
    # pattern. The header between session_id and entity list varies by dungeon type.
    score_offset = _find_score_offset(body)
    struct.pack_into('<i', body, score_offset, score)
    for offset in score_mirror_offsets or []:
        struct.pack_into('<i', body, offset, score)

    return bytes(body)


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

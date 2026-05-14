import datetime
import json

from .serialization import BytesReader, BytesWriter
from .equipment_catalog import (
    CONTENT_TYPE_ARMOR,
    CONTENT_TYPE_WEAPON,
    equipment_display,
    equipment_is_metal as catalog_equipment_is_metal,
    equipment_rarity as catalog_equipment_rarity,
    equipment_slot_name as catalog_equipment_slot_name,
)


def _fmt_time(ms):
    if ms == 0:
        return "(epoch 0)"
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms)


# ==========================================================================
# login/startup
# ==========================================================================

def build_startup_request(startup_mask: bytes, client_uuid: str, terminal_id: str) -> bytes:
    w = BytesWriter()
    w.write_bytes(startup_mask)
    w.write_string(client_uuid)
    w.write_string(terminal_id)
    return w.to_bytes()


def parse_startup_response(data: bytes) -> dict:
    r = BytesReader(data)
    return {
        "_status": r.read_int(),
        "UserId": r.read_long(),
        "AuthorizationKey": r.read_bytes(),
    }


# ==========================================================================
# login/login
# ==========================================================================

def build_login_request(auth_count: int, mask: bytes, client_uuid: str,
                        advertising_id: str = None, is_tracking: bool = None) -> bytes:
    w = BytesWriter()
    w.write_int(auth_count)
    w.write_bytes(mask)
    w.write_string(client_uuid)
    w.write_nullable_string(advertising_id)
    w.write_nullable_bool(is_tracking)
    return w.to_bytes()


def parse_login_response(data: bytes) -> dict:
    r = BytesReader(data)
    result = {
        "_status": r.read_int(),
        "AuthorizationCount": r.read_int(),
        "SessionKey": r.read_bytes(),
        "ClientId": r.read_string(),
        "InGameSessionId": r.read_nullable_long(),
        "PerformanceMetricsEnabled": r.read_bool(),
        "AssetCdnUrl": r.read_string(),
    }
    if r.remaining() > 0:
        result["_remaining"] = r.remaining()
    return result


# ==========================================================================
# masterdata/get_version
# ==========================================================================

def parse_masterdata_response(data: bytes) -> dict:
    r = BytesReader(data)
    return {
        "_status": r.read_int(),
        "timestamp": r.read_int(),
        "revision": r.read_int(),
        "version": r.read_string(),
    }


# ==========================================================================
# terms/terms_agree_eu
# ==========================================================================

def build_terms_agree_request(version1: int = 1, version2: int = 1, flag: bool = False) -> bytes:
    w = BytesWriter()
    w.write_int(version1)
    w.write_int(version2)
    w.write_bool(flag)
    return w.to_bytes()


# ==========================================================================
# home/fetch_info
# ==========================================================================

def build_home_info_request(device_name: str = "iPhone",
                            device_token: str = None,
                            advertising_id: str = None,
                            is_tracking: bool = None,
                            firebase_id: str = None,
                            adjust_id: str = None) -> bytes:
    w = BytesWriter()
    w.write_nullable_string(device_token)
    w.write_string(device_name)
    w.write_nullable_string(advertising_id)
    w.write_nullable_bool(is_tracking)
    w.write_nullable_string(firebase_id)
    w.write_nullable_string(adjust_id)
    return w.to_bytes()


def _read_notice_banner(r: BytesReader) -> dict:
    b = {
        "InformationId": r.read_int(),
        "NoticeType": r.read_int(),
        "Category": r.read_int(),
        "Label": r.read_int(),
        "HomeBannerType": r.read_int(),
        "TransitionRelationMasterId": r.read_nullable_int(),
        "BannerImageUrl": r.read_string(),
    }
    has_header = r.read_bool()
    b["HeaderImageUrl"] = r.read_string() if has_header else None
    b["TitleText"] = r.read_string()
    b["StartAt"] = _fmt_time(r.read_long())
    has = r.read_bool()
    b["EndAt"] = _fmt_time(r.read_long()) if has else None
    has = r.read_bool()
    b["EventEndAt"] = _fmt_time(r.read_long()) if has else None
    b["UpdatedAt"] = _fmt_time(r.read_long())
    has = r.read_bool()
    b["ArchivedAt"] = _fmt_time(r.read_long()) if has else None
    b["IsMandatory"] = r.read_bool()
    b["MandatoryPriority"] = r.read_int()
    b["DisplayOrder"] = r.read_int()
    return b


def parse_home_info_response(data: bytes) -> dict:
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    result["PresentCount"] = r.read_int()
    result["MissionUnreceivedCount"] = r.read_int()

    n = r.read_int()
    result["MissionPanelUnreceivedCountList"] = [
        {"MissionPanelMasterId": r.read_int(), "UnreceivedCount": r.read_int()}
        for _ in range(n)
    ]

    # NoticeHomeInfo
    n_mandatory = r.read_int()
    mandatory = [_read_notice_banner(r) for _ in range(n_mandatory)]
    n_banners = r.read_int()
    home_banners = [_read_notice_banner(r) for _ in range(n_banners)]
    has_new = r.read_bool()
    latest_updated = r.read_long()
    has_survey = r.read_bool()
    survey = _read_notice_banner(r) if has_survey else None
    has_discord = r.read_bool()
    discord = _read_notice_banner(r) if has_discord else None
    result["Notice"] = {
        "MandatoryNotices": mandatory,
        "HomeBannerNotices": home_banners,
        "HasNewNotice": has_new,
        "NewNoticeLatestUpdatedAt": _fmt_time(latest_updated),
        "CbtSurveyNotice": survey,
        "CbtDiscordNotice": discord,
    }

    # LoginBonusReceiveDataList
    n = r.read_int()
    result["LoginBonusReceiveDataList"] = []
    for _ in range(n):
        mid = r.read_int(); day = r.read_int(); start = r.read_long()
        has_end = r.read_bool(); end = r.read_long() if has_end else None
        result["LoginBonusReceiveDataList"].append({
            "LoginBonusMasterId": mid, "Day": day,
            "StartAt": _fmt_time(start), "EndAt": _fmt_time(end) if end else None,
        })

    result["PerformanceMetricsEnabled"] = r.read_bool()

    # UserPointCardList
    n = r.read_int()
    result["UserPointCardList"] = []
    for _ in range(n):
        mid = r.read_int(); start = r.read_long(); end = r.read_long()
        ticket = r.read_int(); after = r.read_int()
        has_exp = r.read_bool(); exp = r.read_long() if has_exp else None
        result["UserPointCardList"].append({
            "PointCardMasterId": mid, "StartAt": _fmt_time(start), "EndAt": _fmt_time(end),
            "TicketAmount": ticket, "AfterPurchaseTicketAmount": after,
            "TicketExpiredAt": _fmt_time(exp) if exp else None,
        })

    # UserPointCardWeeklyList
    n = r.read_int()
    result["UserPointCardWeeklyList"] = []
    for _ in range(n):
        mid = r.read_int(); start = r.read_long(); end = r.read_long()
        result["UserPointCardWeeklyList"].append({
            "PointCardMasterId": mid, "StartAt": _fmt_time(start), "EndAt": _fmt_time(end),
        })

    result["HasReachedUnreceivedPointCardPoint"] = r.read_bool()

    # UserInfoTriggerPointCardRenewedList
    n = r.read_int()
    result["UserInfoTriggerPointCardRenewedList"] = []
    for _ in range(n):
        tid = r.read_long(); mid = r.read_int(); ticket = r.read_int()
        result["UserInfoTriggerPointCardRenewedList"].append({
            "UserTriggerId": tid, "PointCardMasterId": mid, "TicketAmount": ticket,
        })

    # ClearedPlatformAchievementMasterIdList
    n = r.read_int()
    result["ClearedPlatformAchievementMasterIdList"] = [r.read_string() for _ in range(n)]

    result["_UserModelDiff_remaining"] = r.remaining()
    return result


# ==========================================================================
# EmptyResponse (user/delete, noop/noop, etc.)
# ==========================================================================

def parse_empty_response(data: bytes) -> dict:
    r = BytesReader(data)
    return {"_status": r.read_int()}


# ==========================================================================
# UserModelResponse (adventure/read, tutorial/read, feature_intro/read,
#                    profile/set_user_name, avatar/save)
# ==========================================================================

def parse_user_model_response(data: bytes) -> dict:
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    result["_UserModelDiff_remaining"] = r.remaining()
    return result


def _read_content(r: BytesReader) -> dict:
    return {
        "ContentType": r.read_int(),
        "ContentMasterId": r.read_int(),
        "ContentAmount": r.read_int(),
    }


def _read_content_orb(r: BytesReader) -> dict:
    return {
        "OrbMasterId": r.read_int(),
        "OrbRank": r.read_int(),
        "Amount": r.read_int(),
    }


def _read_content_treasure(r: BytesReader) -> dict:
    return {
        "ContentType": r.read_int(),
        "ContentMasterId": r.read_int(),
        "ContentAmount": r.read_int(),
        "MemoryOrbRank": r.read_nullable_int(),
        "TreasureBoxRarity": r.read_int(),
        "IsNew": r.read_bool(),
    }


def _read_list(r: BytesReader, read_item) -> list[dict]:
    return [read_item(r) for _ in range(r.read_int())]


def _read_nullable_list(r: BytesReader, read_item):
    if not r.read_bool():
        return None
    return [read_item(r) for _ in range(r.read_int())]


def _read_time(r: BytesReader) -> int:
    return r.read_long()


def _read_nullable_time(r: BytesReader):
    if not r.read_bool():
        return None
    return _read_time(r)


def _read_localized_text(r: BytesReader) -> str:
    return r.read_string()


def _read_nullable_localized_text(r: BytesReader):
    if not r.read_bool():
        return None
    return _read_localized_text(r)


def _read_nullable_string(r: BytesReader):
    if not r.read_bool():
        return None
    return r.read_string()


def _read_user_enemy_kind(r: BytesReader) -> dict:
    return {
        "EnemyKindMasterId": r.read_int(),
        "KillCount": r.read_long(),
        "KilledAt": _read_time(r),
        "ReceivedMaxKillIndex": r.read_int(),
    }


def _read_user_event_item(r: BytesReader) -> dict:
    return {"EventItemMasterId": r.read_int(), "Amount": r.read_int()}


def _read_user_event_point(r: BytesReader) -> dict:
    return {"EventPointMasterId": r.read_int(), "Point": r.read_int()}


def _read_user_growth_material(r: BytesReader) -> dict:
    return {"GrowthMaterialMasterId": r.read_int(), "Amount": r.read_int()}


def _read_user_orb(r: BytesReader) -> dict:
    return {
        "UserOrbId": r.read_long(),
        "OrbMasterId": r.read_int(),
        "Rank": r.read_int(),
        "RankUpPoint": r.read_int(),
        "IsLock": r.read_bool(),
        "AcquiredAt": _read_time(r),
    }


def _read_user_stage(r: BytesReader) -> dict:
    return {
        "StageMasterId": r.read_int(),
        "ClearCount": r.read_int(),
        "MaxScore": r.read_nullable_int(),
        "MaxClearRank": r.read_nullable_int(),
        "LastPlayAt": _read_time(r),
        "SkipDailyCount": r.read_int(),
        "SkipResetAt": _read_time(r),
    }


def _read_user_style(r: BytesReader) -> dict:
    return {
        "StyleMasterId": r.read_int(),
        "Level": r.read_int(),
        "LevelExp": r.read_int(),
    }


def _read_user_status(r: BytesReader) -> dict:
    return {
        "Name": _read_nullable_localized_text(r),
        "Message": _read_localized_text(r),
        "Gold": r.read_int(),
        "LastLoginAt": _read_time(r),
        "ActionPointFullAt": _read_time(r),
        "ActionPointBroken": r.read_int(),
        "CurrentUsedDeckId": r.read_int(),
        "RankExp": r.read_int(),
        "LastReceivedRank": r.read_int(),
        "InitialRecoveryAt": _read_time(r),
        "ActionPointRecoverSnsCoinCount": r.read_int(),
        "ActionPointRecoverSnsCoinNextResetAt": _read_time(r),
        "LastPlayedAreaDifficulty": r.read_int(),
        "LastPlayedNormalStageMasterId": r.read_nullable_int(),
        "LastPlayedHardStageMasterId": r.read_nullable_int(),
        "PearlPoint": r.read_int(),
        "TutorialStep": r.read_int(),
        "TermsOfUseVersion": r.read_nullable_int(),
        "PrivacyPolicyVersion": r.read_nullable_int(),
        "KoreaPrivacyConsentVersion": r.read_nullable_int(),
        "AgreedTermsAt": _read_time(r),
        "StoryOrbAlbumPoint": r.read_int(),
        "EventOrbAlbumPoint": r.read_int(),
        "StoryEnemyAlbumPoint": r.read_int(),
        "EventEnemyAlbumPoint": r.read_int(),
        "PointCardPoint": r.read_int(),
        "EquipmentPoint": r.read_int(),
        "LastGachaDrawAt": _read_time(r),
        "IsUnderAge": r.read_nullable_bool(),
        "CountryCode": _read_nullable_string(r),
        "FcmToken": _read_nullable_string(r),
        "IsPushNotificationEnable": r.read_nullable_bool(),
        "IsAdchanceOrbEnable": r.read_bool(),
        "IsAdchancePointCardPointEnable": r.read_bool(),
        "CreateUserAt": _read_time(r),
        "ProfileBackgroundMasterId": r.read_int(),
        "ProfileEffectMasterId": r.read_nullable_int(),
        "ProfileFrameMasterId": r.read_int(),
        "ProfileAccoladeMasterId": r.read_int(),
        "ProfilePoseMasterId": r.read_int(),
        "ProfileFacialExpressionMasterId": r.read_int(),
        "ProfileCameraPositionX": r.read_int(),
        "ProfileCameraPositionY": r.read_int(),
        "ProfileCameraSize": r.read_int(),
        "ProfileCameraRotationY": r.read_int(),
        "ProfileAppealRecordType1": r.read_int(),
        "ProfileAppealRecordType2": r.read_int(),
        "ProfileAppealRecordType3": r.read_int(),
        "IsVisibleProfileAppealRecord": r.read_bool(),
        "ProfileEquipmentSettingType": r.read_int(),
        "IsPublishedProfile": r.read_bool(),
        "LikeCount": r.read_int(),
        "BirthMonthId": r.read_nullable_int(),
        "IsDataTransferred": r.read_bool(),
        "DailyAmountLimitAdExpansionCount": r.read_int(),
        "DailyAmountLimitAdIntervalAt": _read_time(r),
        "DailyAmountLimitResetAdExpansionCountAt": _read_time(r),
        "DailyInGameContinueAdCount": r.read_int(),
        "DailyInGameContinueAdCountResetAt": _read_time(r),
        "AdRewardReceivedCount": r.read_int(),
        "HasUserCaution": r.read_bool(),
        "TotalDepositFreeSnsCoin": r.read_int(),
        "TotalDepositBillingSnsCoin": r.read_int(),
        "TotalConsumeFreeSnsCoin": r.read_int(),
        "TotalConsumeBillingSnsCoin": r.read_int(),
    }


def _read_user_sns_coin(r: BytesReader):
    if not r.read_bool():
        return None
    return {"FreeAmount": r.read_int(), "BillingAmount": r.read_int()}


def _read_user_model_int_id(r: BytesReader, field_name: str) -> dict:
    return {field_name: r.read_int()}


def _read_user_model_list(r: BytesReader, read_item):
    return [read_item(r) for _ in range(r.read_int())]


_USER_MODEL_DIFF_LISTS = (
    ("UserAccessoryUpdateList", None),
    ("UserAccessoryDeleteList", "long"),
    ("UserAccessoryUniqueUpdateList", None),
    ("UserAccessoryUniqueDeleteList", "int"),
    ("UserActionPointRecoverItemUpdateList", None),
    ("UserActionPointRecoverItemDeleteList", "int"),
    ("UserAdventureUpdateList", None),
    ("UserAdventureDeleteList", "long"),
    ("UserAmountLimitUpdateList", lambda r: {"AmountLimitType": r.read_int(), "ExpansionAmount": r.read_int()}),
    ("UserAmountLimitDeleteList", "int"),
    ("UserAreaAchievementUpdateList", lambda r: _read_user_model_int_id(r, "AreaAchievementMasterId")),
    ("UserAreaAchievementDeleteList", "int"),
    ("UserArmorUpdateList", lambda r: {
        "UserArmorId": r.read_long(), "ArmorMasterId": r.read_int(), "Level": r.read_int(),
        "LevelExp": r.read_int(), "LimitBreakStep": r.read_int(), "IsLock": r.read_bool(),
        "AcquiredAt": _read_time(r),
    }),
    ("UserArmorDeleteList", "long"),
    ("UserArmorUniqueUpdateList", lambda r: _read_user_model_int_id(r, "ArmorMasterId")),
    ("UserArmorUniqueDeleteList", "int"),
    ("UserAvatarUpdateList", None),
    ("UserAvatarDeleteList", "int"),
    ("UserDeckUpdateList", None),
    ("UserDeckDeleteList", "int"),
    ("UserEnemyAlbumPointRewardUpdateList", lambda r: _read_user_model_int_id(r, "EnemyAlbumPointRewardMasterId")),
    ("UserEnemyAlbumPointRewardDeleteList", "int"),
    ("UserEnemyKindUpdateList", _read_user_enemy_kind),
    ("UserEnemyKindDeleteList", "int"),
    ("UserEventGachaBoxUpdateList", None),
    ("UserEventGachaBoxDeleteList", "int"),
    ("UserEventHuntAreaUpdateList", lambda r: {"EventHuntAreaMasterId": r.read_int(), "IsReadTown": r.read_bool()}),
    ("UserEventHuntAreaDeleteList", "int"),
    ("UserEventHuntEffectUpdateList", lambda r: _read_user_model_int_id(r, "EventHuntEffectMasterId")),
    ("UserEventHuntEffectDeleteList", "int"),
    ("UserEventHuntEventFeatureIntroUpdateList", lambda r: _read_user_model_int_id(r, "EventHuntEventFeatureIntroMasterId")),
    ("UserEventHuntEventFeatureIntroDeleteList", "int"),
    ("UserEventHuntTownGimmickUpdateList", lambda r: _read_user_model_int_id(r, "EventHuntTownGimmickMasterId")),
    ("UserEventHuntTownGimmickDeleteList", "int"),
    ("UserEventItemUpdateList", _read_user_event_item),
    ("UserEventItemDeleteList", "int"),
    ("UserEventPickSkillUpdateList", lambda r: {
        "EventPickSkillMasterId": r.read_int(), "Rank": r.read_int(), "AcquiredAt": _read_time(r),
    }),
    ("UserEventPickSkillDeleteList", "int"),
    ("UserEventPointUpdateList", _read_user_event_point),
    ("UserEventPointDeleteList", "int"),
    ("UserEventPointRewardUpdateList", lambda r: _read_user_model_int_id(r, "EventPointRewardMasterId")),
    ("UserEventPointRewardDeleteList", "int"),
    ("UserEventSpecialEffectUpdateList", lambda r: {
        "EventSpecialEffectMasterId": r.read_int(), "Rank": r.read_int(), "AcquiredAt": _read_time(r),
    }),
    ("UserEventSpecialEffectDeleteList", "int"),
    ("UserEventSurvivalUpdateList", lambda r: {
        "EventSurvivalMasterId": r.read_int(), "TotalScore": r.read_int(),
        "TotalScoreUpdatedAt": _read_time(r), "IsReceivedRankingReward": r.read_bool(),
    }),
    ("UserEventSurvivalDeleteList", "int"),
    ("UserEventSurvivalCasualScoreRewardUpdateList", lambda r: _read_user_model_int_id(r, "EventSurvivalCasualScoreRewardMasterId")),
    ("UserEventSurvivalCasualScoreRewardDeleteList", "int"),
    ("UserExpeditionAreaUpdateList", lambda r: _read_user_model_int_id(r, "ExpeditionAreaId")),
    ("UserExpeditionAreaDeleteList", "int"),
    ("UserExpeditionItemUpdateList", lambda r: {"UserExpeditionItemId": r.read_int(), "Amount": r.read_int()}),
    ("UserExpeditionItemDeleteList", "int"),
    ("UserExpeditionSlotUpdateList", lambda r: {
        "SlotIndex": r.read_int(), "ExpeditionAreaId": r.read_int(), "IsExpedition": r.read_bool(),
        "FinishAt": _read_time(r), "Level": r.read_int(),
    }),
    ("UserExpeditionSlotDeleteList", "int"),
    ("UserFeatureIntroUpdateList", lambda r: _read_user_model_int_id(r, "FeatureIntroType")),
    ("UserFeatureIntroDeleteList", "int"),
    ("UserGachaAutoSellUpdateList", lambda r: _read_user_model_int_id(r, "SellType")),
    ("UserGachaAutoSellDeleteList", "int"),
    ("UserGachaDrawUpdateList", lambda r: {
        "GachaDrawMasterId": r.read_int(), "DrawCount": r.read_int(),
        "TotalDrawCount": r.read_int(), "DrawCountResetAt": _read_time(r),
    }),
    ("UserGachaDrawDeleteList", "int"),
    ("UserGachaStampCardUpdateList", lambda r: {
        "StampCardMasterId": r.read_int(), "Number": r.read_int(),
        "RewardIndex": r.read_int(), "LoopCount": r.read_int(),
    }),
    ("UserGachaStampCardDeleteList", "int"),
    ("UserGachaTicketUpdateList", lambda r: {"GachaTicketMasterId": r.read_int(), "Amount": r.read_int()}),
    ("UserGachaTicketDeleteList", "int"),
    ("UserGrowthMaterialUpdateList", _read_user_growth_material),
    ("UserGrowthMaterialDeleteList", "int"),
    ("UserImportantItemUpdateList", lambda r: {
        "ImportantItemMasterId": r.read_int(), "IsReceivedReward": r.read_bool(), "AcquiredAt": _read_time(r),
    }),
    ("UserImportantItemDeleteList", "int"),
    ("UserInfoTriggerEventSurvivalResultUpdateList", lambda r: {
        "UserTriggerId": r.read_long(), "EventSurvivalMasterId": r.read_int(),
        "Score": r.read_int(), "Rank": r.read_nullable_int(),
    }),
    ("UserInfoTriggerEventSurvivalResultDeleteList", "long"),
    ("UserMainAreaUpdateList", lambda r: {
        "AreaMasterId": r.read_int(), "IsReadedNormalUnlock": r.read_bool(),
        "IsReadedHardUnlock": r.read_bool(), "IsReceivedImportantItemReward": r.read_bool(),
    }),
    ("UserMainAreaDeleteList", "int"),
    ("UserMissionPanelUpdateList", lambda r: {"MissionPanelMasterId": r.read_int(), "CurrentStep": r.read_int()}),
    ("UserMissionPanelDeleteList", "int"),
    ("UserMissionPanelCellUpdateList", lambda r: {
        "MissionPanelCellMasterId": r.read_int(), "StartCount": r.read_int(),
        "ProgressCount": r.read_int(), "ClearedAt": _read_nullable_time(r),
        "ReceivedRewardAt": _read_nullable_time(r),
    }),
    ("UserMissionPanelCellDeleteList", "int"),
    ("UserMissionPanelStepUpdateList", lambda r: {
        "MissionPanelStepMasterId": r.read_int(), "ClearedAt": _read_nullable_time(r),
        "ReceivedRewardAt": _read_nullable_time(r),
    }),
    ("UserMissionPanelStepDeleteList", "int"),
    ("UserNoticeUpdateList", lambda r: {"InformationId": r.read_int(), "LastReadAt": _read_time(r)}),
    ("UserNoticeDeleteList", "int"),
    ("UserOptionalTutorialUpdateList", lambda r: _read_user_model_int_id(r, "OptionalTutorialType")),
    ("UserOptionalTutorialDeleteList", "int"),
    ("UserOrbUpdateList", _read_user_orb),
    ("UserOrbDeleteList", "long"),
    ("UserOrbAlbumPointRewardUpdateList", lambda r: _read_user_model_int_id(r, "OrbAlbumRewardPointMasterId")),
    ("UserOrbAlbumPointRewardDeleteList", "int"),
    ("UserOrbUniqueUpdateList", lambda r: {
        "OrbMasterId": r.read_int(), "MaxRank": r.read_int(),
        "MaxReceivedRewardRank": r.read_nullable_int(), "AcquiredAt": _read_time(r),
    }),
    ("UserOrbUniqueDeleteList", "int"),
    ("UserPearlUpdateList", lambda r: {
        "UserPearlId": r.read_long(), "PearlMasterId": r.read_int(),
        "ExtendedSpecialEffectMasterId": r.read_nullable_int(), "IsLock": r.read_bool(),
        "AcquiredAt": _read_time(r),
    }),
    ("UserPearlDeleteList", "long"),
    ("UserPearlGachaTicketUpdateList", lambda r: {"PearlGachaTicketMasterId": r.read_int(), "Amount": r.read_int()}),
    ("UserPearlGachaTicketDeleteList", "int"),
    ("UserPlayableGuideUpdateList", lambda r: _read_user_model_int_id(r, "PlayableGuideType")),
    ("UserPlayableGuideDeleteList", "int"),
    ("UserPointCardProductUpdateList", lambda r: {"PointCardProductMasterId": r.read_int(), "PurchaseCount": r.read_int()}),
    ("UserPointCardProductDeleteList", "int"),
    ("UserProfileAccoladeUpdateList", lambda r: {"ProfileAccoladeMasterId": r.read_int(), "AcquiredAt": _read_time(r)}),
    ("UserProfileAccoladeDeleteList", "int"),
    ("UserProfileBackgroundUpdateList", lambda r: {"ProfileBackgroundMasterId": r.read_int(), "AcquiredAt": _read_time(r)}),
    ("UserProfileBackgroundDeleteList", "int"),
    ("UserProfileEffectUpdateList", lambda r: {"ProfileEffectMasterId": r.read_int(), "AcquiredAt": _read_time(r)}),
    ("UserProfileEffectDeleteList", "int"),
    ("UserProfileFacialExpressionUpdateList", lambda r: {"ProfileFacialExpressionMasterId": r.read_int(), "AcquiredAt": _read_time(r)}),
    ("UserProfileFacialExpressionDeleteList", "int"),
    ("UserProfileFrameUpdateList", lambda r: {"ProfileFrameMasterId": r.read_int(), "AcquiredAt": _read_time(r)}),
    ("UserProfileFrameDeleteList", "int"),
    ("UserProfilePoseUpdateList", lambda r: {"ProfilePoseMasterId": r.read_int(), "AcquiredAt": _read_time(r)}),
    ("UserProfilePoseDeleteList", "int"),
    ("UserReleaseFunctionUpdateList", lambda r: {"ReleaseFunctionType": r.read_int(), "IsLock": r.read_bool()}),
    ("UserReleaseFunctionDeleteList", "int"),
    ("UserShopExchangeContentUpdateList", lambda r: {
        "ShopExchangeContentMasterId": r.read_int(), "ExchangeCount": r.read_int(),
        "TotalExchangeCount": r.read_int(), "LastExchangeAt": _read_time(r),
    }),
    ("UserShopExchangeContentDeleteList", "int"),
    ("UserStageUpdateList", _read_user_stage),
    ("UserStageDeleteList", "int"),
    ("UserStageAchievementUpdateList", lambda r: _read_user_model_int_id(r, "StageAchievementMasterId")),
    ("UserStageAchievementDeleteList", "int"),
    ("UserStageEnemyUpdateList", lambda r: {"UserStageEnemyId": r.read_long()}),
    ("UserStageEnemyDeleteList", "long"),
    ("UserStagePeriodUpdateList", lambda r: {
        "StageMasterId": r.read_int(), "PlayCount": r.read_int(), "LastPlayAt": _read_time(r),
    }),
    ("UserStagePeriodDeleteList", "int"),
    ("UserStageTicketUpdateList", lambda r: {
        "StageTicketMasterId": r.read_int(), "Amount": r.read_int(), "RecoveryAt": _read_nullable_time(r),
    }),
    ("UserStageTicketDeleteList", "int"),
)


def _read_user_model_diff(r: BytesReader) -> dict:
    result = {}
    for name, reader in _USER_MODEL_DIFF_LISTS:
        if reader == "int":
            result[name] = _read_user_model_list(r, lambda rr: rr.read_int())
        elif reader == "long":
            result[name] = _read_user_model_list(r, lambda rr: rr.read_long())
        elif reader is None:
            count = r.read_int()
            if count:
                raise ValueError(f"unsupported non-empty UserModelDiff list: {name} count={count}")
            result[name] = []
        else:
            result[name] = _read_user_model_list(r, reader)
    result["UserStatus"] = _read_user_status(r)
    result["UserStoreReviewUpdateList"] = _read_user_model_list(r, lambda rr: {
        "TriggerId": rr.read_int(), "TriggerType": rr.read_int(), "TriggerAt": _read_time(rr),
        "RespondedAt": _read_nullable_time(rr), "IsReviewed": rr.read_nullable_bool(),
    })
    result["UserStoreReviewDeleteList"] = _read_user_model_list(r, lambda rr: rr.read_int())
    result["UserStyleUpdateList"] = _read_user_model_list(r, _read_user_style)
    result["UserStyleDeleteList"] = _read_user_model_list(r, lambda rr: rr.read_int())
    result["UserStylePanelUpdateList"] = _read_user_model_list(r, lambda rr: {
        "StylePanelMasterId": rr.read_int(), "Level": rr.read_int(),
    })
    result["UserStylePanelDeleteList"] = _read_user_model_list(r, lambda rr: rr.read_int())
    result["UserStylePanelOrbUpdateList"] = _read_user_model_list(r, lambda rr: {
        "UserStylePanelOrbId": rr.read_long(), "UserOrbId": rr.read_long(), "OrbIndex": rr.read_int(),
    })
    result["UserStylePanelOrbDeleteList"] = _read_user_model_list(r, lambda rr: rr.read_long())
    result["UserWeaponUpdateList"] = _read_user_model_list(r, lambda rr: {
        "UserWeaponId": rr.read_long(), "WeaponMasterId": rr.read_int(), "Level": rr.read_int(),
        "LevelExp": rr.read_int(), "LimitBreakStep": rr.read_int(), "IsLock": rr.read_bool(),
        "AcquiredAt": _read_time(rr),
    })
    result["UserWeaponDeleteList"] = _read_user_model_list(r, lambda rr: rr.read_long())
    result["UserWeaponUniqueUpdateList"] = _read_user_model_list(r, lambda rr: _read_user_model_int_id(rr, "WeaponMasterId"))
    result["UserWeaponUniqueDeleteList"] = _read_user_model_list(r, lambda rr: rr.read_int())
    result["UserSnsCoin"] = _read_user_sns_coin(r)
    return result


def _read_in_game_drop_content(r: BytesReader) -> dict:
    return {
        "ContentType": r.read_nullable_int(),
        "ContentMasterId": r.read_nullable_int(),
        "ContentAmount": r.read_nullable_int(),
        "TreasureBoxRarity": r.read_nullable_int(),
    }


def _read_in_game_block_node(r: BytesReader) -> dict:
    return {
        "UniqueId": r.read_int(),
        "BlockMasterId": r.read_long(),
        "Depth": r.read_int(),
        "SubStageMasterId": r.read_long(),
    }


def _read_in_game_gimmick_placement(r: BytesReader) -> dict:
    return {"GimmickPlacementId": r.read_long()}


def _read_in_game_gimmick_treasure_box(r: BytesReader) -> dict:
    return {
        "GimmickPlacementId": r.read_long(),
        "DropContent": _read_in_game_drop_content(r),
    }


def _read_in_game_gimmick_skill_presenter(r: BytesReader) -> dict:
    return {
        "GimmickPlacementId": r.read_long(),
        "SkillPresenterId": r.read_int(),
    }


def _read_in_game_gimmick_enemy_spawner_mimic(r: BytesReader) -> dict:
    return {
        "GimmickPlacementId": r.read_long(),
        "DropContentList": _read_list(r, _read_in_game_drop_content),
    }


def _read_in_game_gimmick_roulette(r: BytesReader) -> dict:
    return {
        "GimmickPlacementId": r.read_long(),
        "LotCellIndex": r.read_int(),
        "DropContent": _read_in_game_drop_content(r),
        "LotRate": r.read_nullable_int(),
    }


def _read_in_game_block_clear_reward(r: BytesReader) -> dict:
    return {
        "BlockMasterId": r.read_long(),
        "DropContentList": _read_list(r, _read_in_game_drop_content),
    }


def _read_in_game_enemy_drop(r: BytesReader) -> dict:
    return {
        "BlockId": r.read_nullable_long(),
        "TeamId": r.read_long(),
        "GroupIdx": r.read_int(),
        "DropContentList": _read_list(r, _read_in_game_drop_content),
    }


def _read_in_game_fixed_stage_lot(r: BytesReader) -> dict:
    return {
        "BlockNodeList": _read_list(r, _read_in_game_block_node),
        "GimmickPlacementList": _read_list(r, _read_in_game_gimmick_placement),
        "GimmickTreasureBoxList": _read_list(r, _read_in_game_gimmick_treasure_box),
        "GimmickSkillPresenterList": _read_list(r, _read_in_game_gimmick_skill_presenter),
        "GimmickEnemySpawnerMimicList": _read_list(r, _read_in_game_gimmick_enemy_spawner_mimic),
        "GimmickRouletteList": _read_list(r, _read_in_game_gimmick_roulette),
        "BlockClearRewardList": _read_list(r, _read_in_game_block_clear_reward),
        "EnemyDropList": _read_list(r, _read_in_game_enemy_drop),
    }


def parse_in_game_start_response(data: bytes) -> dict:
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    if result["_status"] != 1:
        result["_remaining"] = r.remaining()
        return result
    result["FixedStageLot"] = _read_in_game_fixed_stage_lot(r)
    result["PerformanceMetricsEnabled"] = r.read_bool()
    result["SessionId"] = r.read_long()
    result["InGameRandomSeed"] = r.read_long()
    result["_UserModelDiff_remaining"] = r.remaining()
    return result


def _read_stage_result_event_survival(r: BytesReader) -> dict:
    return {
        "BeforeRank": r.read_nullable_int(),
        "AfterRank": r.read_nullable_int(),
    }


def _read_stage_result(r: BytesReader) -> dict:
    stage_result = {
        "Gold": r.read_int(),
        "StyleExp": r.read_int(),
        "ResultContentList": _read_list(r, _read_content),
        "ResultContentOrbList": _read_list(r, _read_content_orb),
        "RankRewardContentTreasureList": _read_list(r, _read_content_treasure),
        "RankRewardContentList": _read_list(r, _read_content),
        "ResultNewContentList": _read_list(r, _read_content),
        "AdChanceOrbMasterId": r.read_nullable_int(),
        "AdChancePointCardPointAmount": r.read_nullable_int(),
        "IsPostedPresent": r.read_bool(),
    }
    stage_result["EventSurvival"] = None
    if r.read_bool():
        stage_result["EventSurvival"] = _read_stage_result_event_survival(r)
    stage_result["CampaignDropContentList"] = _read_list(r, _read_content)
    return stage_result


def parse_in_game_result_response(data: bytes) -> dict:
    """Parse InGameResultResponse, including UserModelDiff reward/account changes."""
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    result["StageResult"] = _read_stage_result(r)
    result["_remaining_after_stage_result"] = r.remaining()
    try:
        result["CanLikeUserIdList"] = _read_nullable_list(r, lambda rr: rr.read_long())
        result["UserModelDiff"] = _read_user_model_diff(r)
        result["UpdatedAt"] = _read_time(r) if r.remaining() >= 8 else None
        result["MasterDataVersion"] = r.read_string() if r.remaining() >= 4 else None
        result["_remaining_after_user_model_diff"] = r.remaining()
    except Exception as exc:
        result["_user_model_diff_parse_error"] = f"{type(exc).__name__}: {exc}"
        result["_user_model_diff_remaining"] = r.remaining()
    return result


def parse_in_game_stage_skip_response(data: bytes) -> dict:
    """Parse InGameStageSkipResponse enough to detect per-skip ad chance rewards."""
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    result["StageResultList"] = _read_list(r, _read_stage_result)
    result["_UserModelDiff_remaining"] = r.remaining()
    return result


# ==========================================================================
# adventure/read
# ==========================================================================

def build_adventure_read_request(adventure_master_id: int) -> bytes:
    w = BytesWriter()
    w.write_long(adventure_master_id)
    return w.to_bytes()


# ==========================================================================
# tutorial/read
# ==========================================================================

TUTORIAL_STEP_VOICE_SETTING = 20
TUTORIAL_STEP_AVATAR_EDIT = 30
TUTORIAL_STEP_STAGE_PROLOGUE = 40
TUTORIAL_STEP_RESUME_PREV_STAGE_FIRST = 60
TUTORIAL_STEP_STAGE_FIRST = 70
TUTORIAL_STEP_RESUME_PREV_GACHA = 80
TUTORIAL_STEP_GACHA = 90
TUTORIAL_STEP_RESUME_GACHA_RESULT = 110
TUTORIAL_STEP_RESUME_PREV_DECK_EDIT = 120
TUTORIAL_STEP_DECK_EDIT = 130
TUTORIAL_STEP_RESUME_PREV_HOME_UNLOCK = 140
TUTORIAL_STEP_RESUME_HOME_UNLOCK = 150
TUTORIAL_STEP_COMPLETED = 160


def build_tutorial_read_request(tutorial_step: int) -> bytes:
    w = BytesWriter()
    w.write_int(tutorial_step)
    return w.to_bytes()


# ==========================================================================
# feature_intro/read
# ==========================================================================

FEATURE_INTRO_SPECIAL_ATTACK = 1
FEATURE_INTRO_VIRTUAL_PAD = 2
FEATURE_INTRO_IN_GAME_LEVEL_UP = 3
FEATURE_INTRO_SKILL_PRESENTER = 4
FEATURE_INTRO_ELEMENT_TYPE = 5
FEATURE_INTRO_IN_GAME_AUTO = 7
FEATURE_INTRO_UI_LAYOUT = 8
FEATURE_INTRO_STAGE_INFO = 34
FEATURE_INTRO_HOME_MENU = 35


def build_feature_intro_read_request(feature_intro_type: int) -> bytes:
    w = BytesWriter()
    w.write_int(feature_intro_type)
    return w.to_bytes()


# ==========================================================================
# profile/set_user_name
# ==========================================================================

def build_set_user_name_request(name: str) -> bytes:
    w = BytesWriter()
    w.write_string(name)
    return w.to_bytes()


# ==========================================================================
# avatar/save
# ==========================================================================

def build_save_avatar_request(
    avatar_id: int = 1, body_id: int = 1, face_id: int = 1,
    eye_color_id: int = 1, skin_color_id: int = 1,
    hair_id: int = 1, hair_color_id: int = 1, voice_id: int = 1,
) -> bytes:
    w = BytesWriter()
    w.write_int(avatar_id)
    w.write_int(body_id)
    w.write_int(face_id)
    w.write_int(eye_color_id)
    w.write_int(skin_color_id)
    w.write_int(hair_id)
    w.write_int(hair_color_id)
    w.write_int(voice_id)
    return w.to_bytes()


# ==========================================================================
# in_game/start_tutorial, in_game/result_tutorial
# ==========================================================================

def parse_start_tutorial_response(data: bytes) -> dict:
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    result["_remaining"] = r.remaining()
    return result


def parse_result_tutorial_response(data: bytes) -> dict:
    r = BytesReader(data)
    result = {"_status": r.read_int()}
    result["_remaining"] = r.remaining()
    return result


# ==========================================================================
# metric/* (adventure_skip, tutorial, low_fps_prolonged, etc.)
#
# Request:  write_string(json_string)
# Response: same as masterdata/get_version {status, timestamp, revision, version}
# ==========================================================================

def build_metric_adventure_skip_request(adventure_master_id: int, command_index: int) -> bytes:
    w = BytesWriter()
    payload = json.dumps({"kpi": {"adventure_master_id": adventure_master_id, "command_index": command_index}}, separators=(',', ':'))
    w.write_string(payload)
    return w.to_bytes()


def build_metric_tutorial_request() -> bytes:
    w = BytesWriter()
    payload = json.dumps({"kpi": {}}, separators=(',', ':'))
    w.write_string(payload)
    return w.to_bytes()


def build_metric_low_fps_request(current_fps: float, duration: float, scene_id: str) -> bytes:
    w = BytesWriter()
    payload = json.dumps({"misc": {"current_fps": current_fps, "duration": duration, "scene_id": scene_id}}, separators=(',', ':'))
    w.write_string(payload)
    return w.to_bytes()


def parse_metric_response(data: bytes) -> dict:
    """Metric responses share the same format as masterdata/get_version."""
    return parse_masterdata_response(data)


def build_metric_device_request(platform: str, device_tier: str, soc_model: str,
                                device_model: str, system_memory_mb: int) -> bytes:
    w = BytesWriter()
    payload = json.dumps({"misc": {
        "platform": platform, "device_tier": device_tier,
        "soc_model": soc_model, "device_model": device_model,
        "system_memory_mb": system_memory_mb,
    }}, separators=(',', ':'))
    w.write_string(payload)
    return w.to_bytes()


# ==========================================================================
# in_game/start, in_game/result
# ==========================================================================

def build_in_game_start_request(stage_master_id: int, deck_index: int = 1,
                                friend_style_id: int = None) -> bytes:
    """Start a stage battle."""
    w = BytesWriter()
    w.write_int(stage_master_id)
    w.write_int(deck_index)
    if friend_style_id is None:
        w.write_bool(False)
        w.write_bool(False)
    else:
        w.write_bool(True)
        w.write_int(friend_style_id)
    return w.to_bytes()


def build_in_game_surrender_request(in_game_session_id: int, reason: int = 2) -> bytes:
    """Surrender an unfinished battle session.

    Captured client payload shape is:
      int64 InGameSessionId, int32 reason, raw UTF-8 JSON object "{}".
    """
    w = BytesWriter()
    w.write_long(in_game_session_id)
    w.write_int(reason)
    w.buf.extend(b"{}")
    return w.to_bytes()


def build_in_game_result_request(stage_master_id: int = None,
                                  template_stage_id: int = None,
                                  in_game_session_id: int = None,
                                  in_game_start_response: dict = None,
                                  dynamic_rewards: bool = True,
                                  damage_taken: int = None,
                                  damage_taken_count: int = None,
                                  dead_count: int = None,
                                  clear_time: int = None,
                                  raw_body: bytes = None) -> bytes:
    """Submit battle result.

    Either provide raw_body directly, or provide stage_master_id to load
    a captured template (with session_id auto-patched to time.time_ns()).
    Use template_stage_id to reuse another stage's template file.
    """
    if raw_body is not None:
        return raw_body
    if stage_master_id is not None:
        from .battle_templates import load_battle_result
        return load_battle_result(
            stage_master_id,
            template_stage_id,
            in_game_session_id,
            start_response=in_game_start_response,
            dynamic_rewards=dynamic_rewards,
            damage_taken=damage_taken,
            damage_taken_count=damage_taken_count,
            dead_count=dead_count,
            clear_time=clear_time,
        )
    raise ValueError("Must provide either raw_body or stage_master_id")


# ==========================================================================
# gacha/*
# ==========================================================================

# Known gacha pool IDs
GACHA_METAL_10 = 100000202      # 金属10連 (3000 diamonds)
GACHA_NORMAL_10 = 100000104     # 普通10連 (10 tickets)
GACHA_TUTORIAL = 800000101      # Tutorial gacha (free)

def equipment_rarity(content_type: int, content_master_id: int) -> int:
    """Return rarity (1, 2, or 3) from content_type and master_id."""
    if content_type not in (CONTENT_TYPE_ARMOR, CONTENT_TYPE_WEAPON):
        return 0
    return catalog_equipment_rarity(content_master_id)


def equipment_slot(content_type: int, content_master_id: int) -> int:
    """Return slot number from master_id."""
    if content_type == CONTENT_TYPE_ARMOR:
        return (content_master_id % 10000) // 1000   # second digit
    elif content_type == CONTENT_TYPE_WEAPON:
        return (content_master_id % 100000) // 1000  # third digit
    return 0


def equipment_is_metal(content_type: int, content_master_id: int) -> bool:
    """Return metal classification when known from the local equipment catalog."""
    if content_type not in (CONTENT_TYPE_ARMOR, CONTENT_TYPE_WEAPON):
        return False
    return catalog_equipment_is_metal(content_master_id)


def equipment_slot_name(content_type: int, content_master_id: int) -> str:
    """Return human-readable slot name."""
    if content_type not in (CONTENT_TYPE_ARMOR, CONTENT_TYPE_WEAPON):
        return "unknown"
    return catalog_equipment_slot_name(content_master_id)


def equipment_display_name(content_type: int, content_master_id: int) -> str:
    """Return display string. Uses known name from catalog if available."""
    if content_type not in (CONTENT_TYPE_ARMOR, CONTENT_TYPE_WEAPON):
        return f"unknown ({content_master_id})"
    return equipment_display(content_master_id)


def build_gacha_draw_request(gacha_master_id: int) -> bytes:
    w = BytesWriter()
    w.write_int(gacha_master_id)
    return w.to_bytes()


def parse_gacha_draw_response(data: bytes) -> dict:
    """Parse gacha/draw response into structured reward data.

    Each reward contains content_type (110=armor, 100=weapon),
    content_master_id (encodes rarity, slot, and metal/normal),
    a unique user_equipment_id, and whether it is new to the album.
    """
    r = BytesReader(data)
    status = r.read_int()
    reward_count = r.read_int()

    rewards = []
    for _ in range(reward_count):
        content_type = r.read_int()
        content_master_id = r.read_int()
        content_amount = r.read_int()
        user_equipment_id = r.read_nullable_long()  # always populated
        _ = r.read_nullable_long()                   # always null in gacha
        is_new = r.read_bool()

        rarity = equipment_rarity(content_type, content_master_id)
        is_metal = equipment_is_metal(content_type, content_master_id)

        rewards.append({
            "content_type": content_type,
            "content_master_id": content_master_id,
            "content_amount": content_amount,
            "user_equipment_id": user_equipment_id,
            "is_new": is_new,
            "rarity": rarity,
            "is_metal": is_metal,
            "equipment_type": "armor" if content_type == CONTENT_TYPE_ARMOR else "weapon",
            "display": equipment_display_name(content_type, content_master_id),
        })

    return {
        "_status": status,
        "reward_count": reward_count,
        "rewards": rewards,
        "_UserModelDiff_remaining": r.remaining(),
    }


def parse_gacha_fetch_list_response(data: bytes) -> dict:
    """Parse gacha/fetch_list response."""
    r = BytesReader(data)
    status = r.read_int()
    draw_count = r.read_int()
    gacha_count = r.read_int()
    gacha_ids = [r.read_int() for _ in range(gacha_count)]
    return {
        "_status": status,
        "draw_count": draw_count,
        "gacha_ids": gacha_ids,
    }


# ==========================================================================
# deck/save_style_equipment
# ==========================================================================

def build_deck_save_equipment_request(raw_body: bytes) -> bytes:
    """Save equipment to a style in a deck.
    Body structure is complex (int+int+nullable<long>+sub_slots...).
    For now, pass the raw body.
    """
    return raw_body


def build_int_list_request(values: list[int]) -> bytes:
    w = BytesWriter()
    w.write_int(len(values))
    for value in values:
        w.write_int(value)
    return w.to_bytes()


def build_single_int_request(value: int) -> bytes:
    w = BytesWriter()
    w.write_int(value)
    return w.to_bytes()


def build_area_receive_achievement_reward_request(area_achievement_ids: list[int]) -> bytes:
    return build_int_list_request(area_achievement_ids)


def build_mission_get_summary_request() -> bytes:
    return b""


def build_mission_receive_daily_reward_and_progress_reward_request(
    mission_ids: list[int],
    progress_reward_id: int,
) -> bytes:
    return build_int_list_request(mission_ids + [progress_reward_id])


def build_mission_receive_achievement_reward_request(mission_ids: list[int]) -> bytes:
    return build_int_list_request(mission_ids)


def build_mission_receive_event_reward_request(mission_ids: list[int]) -> bytes:
    return build_int_list_request(mission_ids)


def build_mission_receive_daily_reward_request(mission_ids: list[int]) -> bytes:
    return build_int_list_request(mission_ids)


def build_mission_receive_daily_progress_reward_request(mission_ids: list[int]) -> bytes:
    return build_int_list_request(mission_ids)


def build_mission_receive_weekly_reward_request(mission_ids: list[int]) -> bytes:
    return build_int_list_request(mission_ids)


def build_mission_receive_weekly_progress_reward_request(mission_ids: list[int]) -> bytes:
    return build_int_list_request(mission_ids)


def build_mission_panel_fetch_request(mission_panel_master_id: int) -> bytes:
    return build_single_int_request(mission_panel_master_id)


def build_mission_panel_receive_reward_request(mission_panel_master_id: int) -> bytes:
    return build_single_int_request(mission_panel_master_id)


def build_user_rank_receive_reward_request() -> bytes:
    return b""


def build_advertisement_receive_reward_chance_point_card_point_request() -> bytes:
    return b""


def build_advertisement_receive_reward_ad_chance_orb_request(orb_master_id: int = 100007) -> bytes:
    return build_single_int_request(orb_master_id)


def build_expedition_receive_reward_request(expedition_id: int = 1) -> bytes:
    return build_single_int_request(expedition_id)


def build_expedition_do_expedition_request(
    expedition_id: int = 1,
    expedition_master_id: int = 105,
    user_style_id: int = 0,
) -> bytes:
    w = BytesWriter()
    w.write_int(expedition_id)
    w.write_int(expedition_master_id)
    w.write_int(user_style_id)
    return w.to_bytes()


def build_shop_exchange_exchange_request(exchange_master_id: int, count: int = 1) -> bytes:
    w = BytesWriter()
    w.write_int(exchange_master_id)
    w.write_int(count)
    return w.to_bytes()


def build_profile_fetch_request(user_id: int) -> bytes:
    w = BytesWriter()
    w.write_long(user_id)
    return w.to_bytes()


def build_album_receive_orb_rank_reward_request(reward_ids: list[int]) -> bytes:
    return build_int_list_request(reward_ids)


def build_album_receive_enemy_kill_count_reward_request(reward_ids: list[int]) -> bytes:
    return build_int_list_request(reward_ids)


# ==========================================================================
# present/fetch, present/receive
# ==========================================================================

def build_present_receive_request(present_ids: list[int]) -> bytes:
    w = BytesWriter()
    w.write_int(len(present_ids))
    for pid in present_ids:
        w.write_int(pid)
    return w.to_bytes()


# ==========================================================================
# playable_guide/read
# ==========================================================================

def build_playable_guide_read_request(guide_id: int) -> bytes:
    w = BytesWriter()
    w.write_int(guide_id)
    return w.to_bytes()


# ==========================================================================
# notice/fetch_notices, notice/read_all_normal_notices
# ==========================================================================

def build_notice_read_all_normal_notices_request(notice_ids: list[int]) -> bytes:
    w = BytesWriter()
    w.write_int(len(notice_ids))
    for notice_id in notice_ids:
        w.write_int(notice_id)
    return w.to_bytes()


# ==========================================================================
# notice/fetch_notice_detail
# ==========================================================================

def build_notice_detail_request(notice_id: int) -> bytes:
    w = BytesWriter()
    w.write_int(notice_id)
    return w.to_bytes()


# ==========================================================================
# billing/update_web_store, release_function/unlock
# ==========================================================================

def build_release_function_unlock_request(function_id: int) -> bytes:
    w = BytesWriter()
    w.write_int(function_id)
    return w.to_bytes()


def build_main_area_read_unlock_request(area_master_id: int, area_difficulty: int) -> bytes:
    w = BytesWriter()
    w.write_int(area_master_id)
    w.write_int(area_difficulty)
    return w.to_bytes()


def build_weapon_growth_level_request(
    user_weapon_id: int,
    consume_content_list: list[tuple[int, int, int]],
) -> bytes:
    w = BytesWriter()
    w.write_long(user_weapon_id)
    w.write_int(len(consume_content_list))
    for content_type, content_master_id, content_amount in consume_content_list:
        w.write_int(content_type)
        w.write_int(content_master_id)
        w.write_int(content_amount)
    return w.to_bytes()

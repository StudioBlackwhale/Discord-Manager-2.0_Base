"""Discord Manager 2.0 기본 설정.

새 방송인용으로 사용할 때는 가급적 이 파일의 값만 수정하세요.
main.py의 구조/권한 로직과 이름/문구를 분리해 두었습니다.
"""

BOT_LOGGER_NAME = "discord_manager"

ROLE_NAMES = {
    "owner": "Owner",
    "manager": "Manager",
    "streamer": "Streamer",
    "viewer": "Viewer",
}

CATEGORY_NAMES = {
    "info": "안내",
    "community": "커뮤니티",
    "collab": "합방",
    "ops": "운영",
}

CHANNEL_NAMES = {
    "rules": "규칙",
    "notice": "공지",
    "broadcast": "방송공지",
    "chat": "자유채팅",
    "media": "사진 및 영상",
    "nickname": "닉네임 제출",
    "waiting": "시참 대기실",
    "participation_1": "시참",
    "participation_2": "시참 예비용",
    "collab_text": "합방 채팅",
    "collab_1": "합방",
    "collab_2": "합방 예비용",
    "staff": "운영관리",
    "nickname_db": "닉네임_DB",
    "audit_log": "관리기록",
}

CHANNEL_TOPICS = {
    "nickname": "아래 닉네임 제출 버튼 또는 /닉네임 명령으로 제출해 주세요. 제출 내용은 관리자만 확인할 수 있습니다.",
}

NICKNAME_GUIDE_TEXT = (
    "게임 닉네임 또는 게임태그를 제출하려면 아래 버튼을 눌러 주세요. "
    "제출한 내용은 관리자만 확인할 수 있으며 다른 멤버에게는 공개되지 않습니다."
)
NICKNAME_BUTTON_LABEL = "닉네임 제출"
NICKNAME_SUBMIT_CUSTOM_ID = "discord-manager:nickname:submit:v1"
NICKNAME_MODAL_TITLE = "닉네임 제출"
NICKNAME_FIELD_LABEL = "방송에서 불릴 닉네임"
NICKNAME_FIELD_PLACEHOLDER = "예) 시청자123"
GAME_FIELD_LABEL = "게임명 (선택)"
NICKNAME_SUCCESS_WITH_GAME = "{game} 닉네임 {nickname} 제출이 완료되었습니다."
NICKNAME_SUCCESS_NO_GAME = "닉네임 {nickname} 제출이 완료되었습니다."

# 닉네임 안내를 찾을 때 전체 채널 기록을 무제한으로 훑지 않습니다.
GUIDE_SCAN_LIMIT = 100

# reset 시에도 유지할 역할입니다.
PROTECTED_ROLE_KEYS = ("owner", "manager", "streamer", "viewer")

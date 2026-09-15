import unittest

import config
import main


class NeutralBaseTests(unittest.IsolatedAsyncioTestCase):
    def test_default_roles_are_neutral(self):
        self.assertEqual(
            config.ROLE_NAMES,
            {
                "owner": "Owner",
                "manager": "Manager",
                "streamer": "Streamer",
                "viewer": "Viewer",
            },
        )

    def test_requested_channel_names(self):
        self.assertEqual(config.CHANNEL_NAMES["media"], "사진 및 영상")
        self.assertEqual(config.CHANNEL_NAMES["participation_2"], "시참 예비용")
        self.assertEqual(config.CHANNEL_NAMES["collab_2"], "합방 예비용")

    def test_text_channel_space_normalization(self):
        self.assertEqual(main.text_key("사진 및 영상"), main.text_key("사진-및-영상"))

    def test_nickname_copy_has_clean_optional_game_spacing(self):
        with_game = config.NICKNAME_SUCCESS_WITH_GAME.format(
            game="League of Legends", nickname="nickname"
        )
        without_game = config.NICKNAME_SUCCESS_NO_GAME.format(nickname="nickname")
        self.assertEqual(
            with_game,
            "League of Legends 닉네임 nickname 제출이 완료되었습니다.",
        )
        self.assertEqual(without_game, "닉네임 nickname 제출이 완료되었습니다.")
        self.assertNotIn("  ", without_game)

    async def test_nickname_view_is_persistent(self):
        view = main.NicknameSubmissionView()
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].custom_id, config.NICKNAME_SUBMIT_CUSTOM_ID)
        self.assertEqual(view.children[0].label, config.NICKNAME_BUTTON_LABEL)

    def test_top_level_commands_remain_three(self):
        names = {command.name for command in main.bot.tree.get_commands()}
        self.assertEqual(names, {"setup", "방송공지", "닉네임"})


if __name__ == "__main__":
    unittest.main()

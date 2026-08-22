"""Tests for the pure, SDK-call-free pieces of `engine.py`: the
`build_user_message` stream-json builder and the `ImagePart` dataclass it
consumes.

`engine.py` imports `claude_agent_sdk` at module top, so -- like
`test_bridge_filter.py` -- this module follows `test_isolation.py`'s
skipUnless pattern: it collects cleanly under system Python (skipped) and
runs fully under the project's venv, even though the functions under test
here never touch the SDK at call time.
"""

import unittest

try:
    from disclaude.engine import ImagePart, build_user_message

    HAS_RUNTIME_DEPS = True
except ImportError:
    HAS_RUNTIME_DEPS = False


@unittest.skipUnless(
    HAS_RUNTIME_DEPS, "requires the venv (discord.py + claude-agent-sdk installed)"
)
class BuildUserMessageTests(unittest.TestCase):
    def test_text_and_images_shape(self):
        images = [
            ImagePart(media_type="image/png", data_b64="AAAA"),
            ImagePart(media_type="image/jpeg", data_b64="BBBB"),
        ]

        message = build_user_message("what are these?", images)

        self.assertEqual(
            message,
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "AAAA",
                            },
                        },
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": "BBBB",
                            },
                        },
                        {"type": "text", "text": "what are these?"},
                    ],
                },
                "parent_tool_use_id": None,
            },
        )

    def test_images_only_has_no_text_block(self):
        images = [ImagePart(media_type="image/png", data_b64="AAAA")]

        message = build_user_message("", images)

        content = message["message"]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "image")
        self.assertTrue(all(block["type"] == "image" for block in content))

    def test_no_session_id_field(self):
        message = build_user_message("hi", [ImagePart(media_type="image/png", data_b64="AAAA")])

        self.assertNotIn("session_id", message)

    def test_image_blocks_precede_text_block(self):
        images = [
            ImagePart(media_type="image/png", data_b64="AAAA"),
            ImagePart(media_type="image/png", data_b64="BBBB"),
            ImagePart(media_type="image/png", data_b64="CCCC"),
        ]

        message = build_user_message("describe these", images)

        content = message["message"]["content"]
        self.assertEqual(content[-1]["type"], "text")
        self.assertTrue(all(block["type"] == "image" for block in content[:-1]))


if __name__ == "__main__":
    unittest.main()

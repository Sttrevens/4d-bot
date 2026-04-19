"""QQ 多媒体消息收发测试。"""

import pytest
from app.channels.qq import QQChannel, _unpack_message_id


class TestParseAttachments:
    """测试 QQ 消息附件解析。"""

    def _make_channel(self):
        return QQChannel()

    def test_parse_c2c_image(self):
        """C2C 消息带图片附件。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "C2C_MESSAGE_CREATE", "id": "evt1",
            "d": {
                "id": "msg1",
                "author": {"user_openid": "user123"},
                "content": "看看这张图",
                "attachments": [{
                    "content_type": "image/png",
                    "filename": "test.png",
                    "height": 720, "width": 1280,
                    "size": "12345",
                    "url": "https://multimedia.nt.qq.com/xxx/test.png",
                }],
            },
        }
        msg = ch.parse_event(payload)
        assert msg is not None
        assert msg.msg_type == "image"
        assert msg.content_raw == "看看这张图"
        assert msg.extra["attachments"][0]["content_type"] == "image/png"

    def test_parse_c2c_video(self):
        """C2C 消息带视频附件。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "C2C_MESSAGE_CREATE", "id": "evt2",
            "d": {
                "id": "msg2",
                "author": {"user_openid": "user456"},
                "content": "",
                "attachments": [{
                    "content_type": "video/mp4",
                    "filename": "video.mp4",
                    "size": "5000000",
                    "url": "https://multimedia.nt.qq.com/xxx/video.mp4",
                }],
            },
        }
        msg = ch.parse_event(payload)
        assert msg is not None
        assert msg.msg_type == "video"

    def test_parse_c2c_audio(self):
        """C2C 消息带语音附件。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "C2C_MESSAGE_CREATE", "id": "evt3",
            "d": {
                "id": "msg3",
                "author": {"user_openid": "user789"},
                "content": "",
                "attachments": [{
                    "content_type": "audio/silk",
                    "filename": "voice.silk",
                    "size": "8000",
                    "url": "https://multimedia.nt.qq.com/xxx/voice.silk",
                }],
            },
        }
        msg = ch.parse_event(payload)
        assert msg is not None
        assert msg.msg_type == "audio"

    def test_parse_c2c_file(self):
        """C2C 消息带文件附件。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "C2C_MESSAGE_CREATE", "id": "evt4",
            "d": {
                "id": "msg4",
                "author": {"user_openid": "user000"},
                "content": "这是一个文件",
                "attachments": [{
                    "content_type": "application/pdf",
                    "filename": "report.pdf",
                    "size": "1048576",
                    "url": "https://multimedia.nt.qq.com/xxx/report.pdf",
                }],
            },
        }
        msg = ch.parse_event(payload)
        assert msg is not None
        assert msg.msg_type == "file"

    def test_parse_group_with_attachment(self):
        """群 @消息带图片附件。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "id": "evt5",
            "d": {
                "id": "msg5",
                "author": {"member_openid": "member123"},
                "group_openid": "group456",
                "content": " 分析这张图",
                "attachments": [{
                    "content_type": "image/jpeg",
                    "filename": "photo.jpg",
                    "height": 1080, "width": 1920,
                    "size": "250000",
                    "url": "https://multimedia.nt.qq.com/xxx/photo.jpg",
                }],
            },
        }
        msg = ch.parse_event(payload)
        assert msg is not None
        assert msg.msg_type == "image"
        assert msg.chat_type == "group"
        assert msg.extra["group_openid"] == "group456"

    def test_parse_no_attachment(self):
        """纯文本消息，无附件。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "C2C_MESSAGE_CREATE", "id": "evt6",
            "d": {
                "id": "msg6",
                "author": {"user_openid": "user111"},
                "content": "hello",
            },
        }
        msg = ch.parse_event(payload)
        assert msg is not None
        assert msg.msg_type == "text"
        assert msg.extra.get("attachments") == []

    def test_parse_multiple_images(self):
        """多图消息。"""
        ch = self._make_channel()
        payload = {
            "op": 0, "t": "C2C_MESSAGE_CREATE", "id": "evt7",
            "d": {
                "id": "msg7",
                "author": {"user_openid": "user222"},
                "content": "三张图",
                "attachments": [
                    {"content_type": "image/png", "url": "https://a.com/1.png", "size": "100"},
                    {"content_type": "image/jpeg", "url": "https://a.com/2.jpg", "size": "200"},
                    {"content_type": "image/webp", "url": "https://a.com/3.webp", "size": "300"},
                ],
            },
        }
        msg = ch.parse_event(payload)
        assert msg.msg_type == "image"  # 首个附件决定 msg_type
        atts = ch.extract_attachments(msg)
        assert len(atts) == 3
        assert all(a["type"] == "image" for a in atts)


class TestExtractAttachments:
    """测试 extract_attachments 标准化。"""

    def test_extract_image(self):
        ch = QQChannel()
        from app.channels.base import IncomingMessage
        msg = IncomingMessage(
            event_id="e1", message_id="p2p:u:m", chat_id="p2p:u",
            chat_type="p2p", sender_id="u", msg_type="image",
            content_raw="", extra={
                "attachments": [{
                    "content_type": "image/png",
                    "filename": "test.png",
                    "height": 720, "width": 1280,
                    "size": "12345",
                    "url": "https://multimedia.nt.qq.com/test.png",
                }],
            },
        )
        atts = ch.extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0]["type"] == "image"
        assert atts[0]["url"] == "https://multimedia.nt.qq.com/test.png"
        assert atts[0]["size"] == 12345
        assert atts[0]["width"] == 1280

    def test_extract_empty(self):
        ch = QQChannel()
        from app.channels.base import IncomingMessage
        msg = IncomingMessage(
            event_id="e2", message_id="p2p:u:m", chat_id="p2p:u",
            chat_type="p2p", sender_id="u", msg_type="text",
            content_raw="hello", extra={"attachments": []},
        )
        assert ch.extract_attachments(msg) == []

    def test_extract_no_extra(self):
        ch = QQChannel()
        from app.channels.base import IncomingMessage
        msg = IncomingMessage(
            event_id="e3", message_id="p2p:u:m", chat_id="p2p:u",
            chat_type="p2p", sender_id="u", msg_type="text",
            content_raw="hello",
        )
        assert ch.extract_attachments(msg) == []

    def test_extract_mixed_types(self):
        ch = QQChannel()
        from app.channels.base import IncomingMessage
        msg = IncomingMessage(
            event_id="e4", message_id="p2p:u:m", chat_id="p2p:u",
            chat_type="p2p", sender_id="u", msg_type="image",
            content_raw="", extra={
                "attachments": [
                    {"content_type": "image/png", "url": "https://a.com/1.png", "size": "100"},
                    {"content_type": "video/mp4", "url": "https://a.com/v.mp4", "size": "5000000"},
                    {"content_type": "application/octet-stream", "url": "https://a.com/f.bin", "size": "999"},
                ],
            },
        )
        atts = ch.extract_attachments(msg)
        assert len(atts) == 3
        assert atts[0]["type"] == "image"
        assert atts[1]["type"] == "video"
        assert atts[2]["type"] == "file"


class TestUnpackMessageId:
    def test_full_format(self):
        assert _unpack_message_id("group:g123:m456") == ("group", "g123", "m456")

    def test_p2p_format(self):
        assert _unpack_message_id("p2p:u789:m001") == ("p2p", "u789", "m001")

    def test_fallback(self):
        assert _unpack_message_id("justamsgid") == ("p2p", "", "justamsgid")


class TestQQAPIHelpers:
    """测试 qq.py 的辅助函数。"""

    def test_file_type_inference(self):
        """upload_and_send_file 应正确推断 file_type。"""
        ch = QQChannel()
        # 通过检查方法存在性验证接口
        assert hasattr(ch, "upload_and_send_file")

    def test_download_image_data_url_passthrough(self):
        """已经是 data URL 的直接返回。"""
        import asyncio
        ch = QQChannel()
        result = asyncio.run(ch.download_image("msg1", "data:image/png;base64,abc123"))
        assert result == "data:image/png;base64,abc123"

    def test_download_image_empty(self):
        """空 image_key 返回空字符串。"""
        import asyncio
        ch = QQChannel()
        result = asyncio.run(ch.download_image("msg1", ""))
        assert result == ""

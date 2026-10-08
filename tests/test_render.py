from __future__ import annotations

from feishu_bridge.im.feishu import render


def test_tables_become_mobile_friendly_lists():
    source = "| 名称 | 状态 |\n| --- | --- |\n| 构建 | 通过 |\n| 测试 | 失败 |"
    result = render.sanitize(source)
    assert "|" not in result
    assert "构建" in result and "状态: 通过" in result


def test_fenced_tables_are_left_alone():
    source = "```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```"
    assert "| 1 | 2 |" in render.sanitize(source)


def test_local_image_paths_become_placeholders():
    source = "看这张图 ![截屏](D:\\shots\\a.png) 和 ![](/tmp/b.png)"
    result = render.sanitize(source)
    assert "D:\\shots" not in result and "/tmp/b.png" not in result
    assert "[图片" in result


def test_local_file_links_keep_their_label():
    result = render.sanitize("见 [报告](/tmp/report.md)")
    assert result.strip() == "见 报告"


def test_chunking_splits_on_paragraphs():
    text = "\n\n".join(["x" * 50] * 10)
    blocks = render.chunk(text, size=120)
    assert len(blocks) > 1
    assert all(len(block) <= 120 for block in blocks)


def test_headings_are_flattened_for_cards():
    assert "标题" in render.sanitize("## 标题")
    assert "#" not in render.sanitize("## 标题")


def test_text_card_shape():
    card = render.text_card("你好")
    assert card["elements"][0]["tag"] == "markdown"
    assert card["elements"][0]["content"] == "你好"

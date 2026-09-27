from app.schemas.knowledge import KnowledgeSearchResponse


def answer_card(response: KnowledgeSearchResponse) -> dict:
    answer = response.answer.split("\n\n参考来源：", 1)[0]
    elements = [{"tag": "markdown", "content": answer}]
    if response.references:
        elements.append({"tag": "markdown", "content": "**引用来源**（点击可查看原文片段）"})
        for index, ref in enumerate(response.references[:5], start=1):
            title = ref.doc_title
            if ref.title_path and ref.title_path != title:
                title += f" · {ref.title_path}"
            elements.append({"tag": "action", "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"来源 {index}：{title[:35]}"},
                "value": {"module": "knowledge", "operation": "source",
                          "doc_id": ref.doc_id, "version": ref.doc_version,
                          "chunk_index": ref.chunk_index},
            }]})
    else:
        elements.append({"tag": "markdown", "content": "可以换一种问法，或补充制度名称后再查询。"})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {
            "tag": "plain_text", "content": "知识库答复"}},
        "elements": elements,
    }


def source_card(title: str, path: str, content: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {
            "tag": "plain_text", "content": f"来源：{title[:40]}"}},
        "elements": [{"tag": "markdown", "content":
                      f"**位置：** {path}\n\n{content[:3500]}"}],
    }

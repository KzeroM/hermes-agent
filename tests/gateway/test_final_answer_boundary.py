from gateway.config import Platform
from gateway.run import _sanitize_gateway_final_response


def test_appended_child_final_answer_is_not_delivered_after_parent_final():
    text = (
        "현재 판정: BLOCKED\n\n"
        "<final_answer>\n"
        "충돌하는 하위 작업 판정: 완료\n"
        "</final_answer>"
    )

    assert _sanitize_gateway_final_response(Platform.TELEGRAM, text) == "현재 판정: BLOCKED"


def test_wrapped_only_final_answer_is_unwrapped_for_chat_delivery():
    text = "<final_answer>\n최종 결과입니다.\nfinal_answer>"

    assert _sanitize_gateway_final_response(Platform.TELEGRAM, text) == "최종 결과입니다."


def test_normal_final_response_is_unchanged():
    text = "정상적인 최종 답변입니다."

    assert _sanitize_gateway_final_response(Platform.TELEGRAM, text) == text

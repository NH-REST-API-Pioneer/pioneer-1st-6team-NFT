"""전략 자동 실행기(M4b). 승인받은 전략을 띄워 둔 컨테이너에서 돌리고, 목표를 `services`로 주문한다.

계층: `runner → services`. `markets`·`realtime`·`nh`·`guard`는 직접 부르지 않는다(pyproject 계약).
"""

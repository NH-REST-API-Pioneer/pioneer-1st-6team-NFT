# R02 · (`R07` 로 병합됨) 8개 REST API 개별 명세의 `Content-Type` 샘플 빈칸

- 상태: **[R07-표기-모음.md](R07-표기-모음.md) 1번 항목으로 병합** (단독 제출하지 않음)
- 사유: 공통 개요(`/#/api/info/response-overview`)에 이미 `Content-Type: application/json`이 명시돼 있어 단독 제보보다는 문서 누락·표기 오류 일괄 정비(`R07`)에 포함하는 편이 적절함.
- 수치 보정 반영: 웹·엑셀 전수 집계 결과 `application/json` 샘플이 채워진 POST REST API는 `접근토큰 폐기` 포함 **32개**(기존 31개 오기 수정), 엑셀 빈칸은 **REST 42개 기준 9개**(실시간 8개 포함 시 17개)로 `R07`에 반영함.
- 검증 로그: `logs/sector0_20261120_231413.json` (D 항목), `check_sector0.py`.

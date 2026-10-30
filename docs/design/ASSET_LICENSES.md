# Asset licenses

Every icon, image, font, illustration, animation, and code snippet needs a row here before it is used. See `DESIGN.md` section 12.

Rules:
- Unknown or unclear license means not approved.
- Free does not mean commercial use is allowed.
- A repository's license does not cover third-party assets inside it. Check each separately.
- AI-generated assets also need review for third-party rights and tool terms.
- Do not record "no legal issue" without a real legal review.

Approval status values: `not approved` (default), `approved`, `rejected`.

| Asset | Type | Source and rights holder | License or terms | Commercial use | Modify / redistribute / attribution | Checked on and evidence | Approval status |
|---|---|---|---|---|---|---|---|

| 돋보기 SVG 아이콘 | Icon | 이번 요청에서 직접 작성, `shell.js` | 외부 자산 미사용 | 외부 라이선스 조건 없음 | 프로젝트 코드와 함께 관리 | 2026-10-28: 사용자 돋보기 버튼 요청 및 `shell.js` 변경 승인 | approved |
| 화면 돋보기 구현 | Code | 이번 요청에서 독립 구현, `shell.js`, `shell.css` | 참고 프로젝트 코드·패키지 미사용 | 외부 라이선스 조건 없음 | 프로젝트 코드와 함께 관리 | 2026-10-28: 사용자 기능 요청 및 두 파일 변경 승인 | approved |
| 수협 로고 (`suhyup-logo.png`) | Image | 수협중앙회 상표 — Figma AppHeader(파이널 프로젝트_수정 2119:2529)의 로고를 PNG 3배로 내보냄, `shell.js` 상단 바 | 상표권은 수협중앙회에 있음, 사용 허락 받지 않음 | 불가 (교육 과정 비상업 시연용으로만) | 수정·재배포 불가, 시연 외 사용 시 수협 허락 필요 | 2026-10-29: 사용자가 KB-Bridge 과정 비상업 시연 목적으로 사용 결정 — 법률 검토 아님 | approved |
| Noto Sans KR | Font | Google Fonts (Google, Sandoll) — `figma-theme.css` `@import` | SIL Open Font License 1.1 | 허용 (OFL) | 수정·재배포 가능, 글꼴 단독 판매 불가, 라이선스 표기 유지 | 2026-10-28: https://fonts.google.com/noto/specimen/Noto+Sans+KR (OFL 표기) 확인 — 법률 검토 아님 | approved |
| Pretendard Black (900), v1.3.9 | Font | 길형진 및 Pretendard 기여자 — 공식 배포 jsDelivr, `figma-theme.css` `@import` | SIL Open Font License 1.1 | 허용 (OFL) | 수정·재배포 가능, 글꼴 단독 판매 불가, 재배포 시 저작권·라이선스 표기 유지 | 2026-10-29: https://github.com/orioncactus/pretendard/blob/v1.3.9/LICENSE 확인, 사용자 글꼴 변경 요청 및 앞선 라이선스 목록 추가 승인 — 법률 검토 아님 | approved |
| Lucide 아이콘 1.63.0 (layout-dashboard · clipboard-list · chart-line · users) | Icon | Lucide 기여자 (일부는 Feather, Cole Bemis) — SVG 경로를 `shell.js` 왼쪽 메뉴에 직접 넣음 | ISC License (Feather 유래 부분은 MIT) | 허용 (ISC·MIT) | 수정·재배포 가능, 재배포 시 저작권·라이선스 표기 유지 | 2026-10-30: https://github.com/lucide-icons/lucide/blob/main/LICENSE 확인, 사용자 기록 요청 — 법률 검토 아님 | approved |

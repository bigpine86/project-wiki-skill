# Project Wiki Skill

개발하면서 무엇을 했고, 왜 선택했고, 결과가 어땠는지 개인 openNAMU 위키에 남기는 Codex 스킬이다. 커밋과 Entire/Codex 근거를 날짜별 일지·프로젝트 설명·중요한 의사결정으로 연결한다.

일상 커밋에는 핵심 편집 기준만 전달한다. 생애주기·구조 개편·사용량의 상세 지침은 필요할 때 읽는다. 숫자는 실행기가 계산하며 글쓰기 모델은 합계를 만들지 않는다.

## 구성

| 경로 | 역할 |
| --- | --- |
| [.agents/skills/project-wiki](.agents/skills/project-wiki/SKILL.md) | 프로젝트별 일지와 현재 설명 편집 |
| [.agents/skills/devlog-wiki](.agents/skills/devlog-wiki/SKILL.md) | 기존 커밋 수집·재시도와 호환 모드 |
| [scripts/project_wiki.py](scripts/project_wiki.py) | 문서 조회·계획·검증·관리 문단 저장 |
| [scripts/devlog_wiki.py](scripts/devlog_wiki.py) | 커밋·Entire 근거 수집, 모델 호출, post-commit 연결 |
| [scripts/wiki_usage.py](scripts/wiki_usage.py) | Codex 관측 사용량 수집·중복 제거·집계 |
| [test](test/) | 외부 모델·네트워크 없이 실행하는 45개 검사 |

이 저장소는 스킬과 실행기 배포본이다. openNAMU 서버·MCP 서버·위키 데이터는 별도로 준비한다. 복제만으로 Git 훅을 설치하거나 위키를 수정하지 않는다.

## 준비

- Git, Python 3.10 이상, 로그인된 Codex CLI.
- 연결 대화 수집에 사용할 Entire CLI. 요청별 사용량은 연결된 Codex 로컬 로그에서 관측한다.
- 개인 MCP 브리지가 설치된 openNAMU 저장소와 실행 중인 위키.
- 위키 저장소의 `scripts/run_personal_mcp.sh`, 빌드된 `bin/personal_mcp`, 비공개 `data/personal_mcp.json`.
- MCP가 제공하는 `wiki_search`, `wiki_read`, `wiki_save`, `wiki_history`, `wiki_backlinks`와 리비전 검사 계약.

현재 연결 대상은 [원본 개인 위키](https://github.com/bigpine86/namuwiki_261020)의 MCP 구현이다. 일반 openNAMU에 공식 MCP가 내장되어 있다고 가정하지 않는다. 현재 어댑터는 숫자 loopback HTTP 주소만 허용한다. 검증 환경은 macOS이며 위키 연결에는 기존 셸 실행 스크립트를 사용한다.

## 설치와 사용

```sh
git clone https://github.com/bigpine86/project-wiki-skill.git
cd project-wiki-skill
mkdir -p tmp
```

실행기는 상대 위치의 스킬 파일을 읽으므로 이 저장소의 구조를 유지한다. 이 폴더에서 Codex를 사용하거나, 다른 프로젝트에서는 아래 스킬의 절대 경로를 지정해 요청할 수 있다.

> `/path/to/project-wiki-skill/.agents/skills/project-wiki/SKILL.md`를 사용해서 이번 작업과 선택 이유를 위키에 기록해줘.

대상 개발 저장소를 등록한다. `main`은 실제 기준 브랜치로, 위키 경로는 MCP가 설치된 저장소로 바꾼다. 이 명령은 대상 저장소에 설정과 post-commit 연결을 설치하므로 사용할 프로젝트에만 실행한다.

```sh
python3 scripts/project_wiki.py --repo /path/to/development-project install \
  --project '예시 프로젝트' --project-id example-project \
  --base-ref main --wiki-repo /path/to/opennamu

python3 scripts/project_wiki.py --repo /path/to/development-project status
```

이후 커밋은 기존 훅 경로로 기록된다. 이미 자동 처리한 입력을 수동으로 중복 발행하지 않는다. 이 저장소를 이동하면 훅이 참조하는 실행기 경로를 다시 설치해야 한다.

커밋 없는 메모는 UTF-8 파일로 준비하고 미리보기 후 저장할 수 있다. 기본 명령은 미리보기이며 `--apply`를 붙이면 저장한다. 사용자가 이미 저장을 요청한 경우 불필요하게 두 번 생성할 필요는 없다. 계획 생성에는 Codex 사용량이 발생한다.

```sh
python3 scripts/project_wiki.py --repo /path/to/development-project record \
  --input /path/to/note.txt --id design-note --date 2026-10-29

python3 scripts/project_wiki.py --repo /path/to/development-project record \
  --input /path/to/note.txt --id design-note --date 2026-10-29 --apply
```

사용량 조회와 사용량 문단 갱신은 추가 모델 호출 없이 실행한다.

```sh
python3 scripts/project_wiki.py --repo /path/to/development-project usage
python3 scripts/project_wiki.py --repo /path/to/development-project usage \
  --refresh --date 2026-10-29 --apply
```

더 필요한 작업에만 [실행 안내](.agents/skills/project-wiki/references/integration.md) 또는 [사용량 기준](.agents/skills/project-wiki/references/usage-recording.md)을 읽는다. 운영 위키의 등록 설정은 이 배포 과정에서 바꾸지 않았다.

## 보존과 현재 한계

- 사람의 문장과 기존 근거를 보존한다. 자동 관리 문단에 사람 수정이 있으면 덮어쓰지 않고 검토 대상으로 남긴다.
- 원장·대기 입력·편집 기준은 대상 개발 저장소의 Git 관리 폴더 `devlog-wiki/`에 있다. 소스 푸시로 백업되지 않는다. 위키 DB와 이 상태를 각각 비공개 백업한다.
- 사용량은 관측된 부분 합계다. 전체 청구액이나 구독 한도를 나타내지 않는다. 여러 세션의 경계가 다를 때 귀속이 미확인으로 남는 알려진 제한이 있다.
- 여러 PC 원장 병합, 중앙 DB 원장, ADR 자동 채번, 문서 이동·이력 병합은 제공하지 않는다.
- 전체 대화·내부 추론·인증 정보는 이 저장소와 사용량 원장에 넣지 않는다.

[중앙 저장 설계](docs/USAGE_STORAGE_DESIGN.md)는 다음 단계 제안이다. 일상 기록마다 읽는 지침이 아니다.

## 검증

```sh
mkdir -p tmp
python3 -m unittest discover -s test -p 'test_*wiki.py'
```

실제 위키와 모델을 호출하지 않는 테스트다. 운영 환경의 로그인·MCP 연결·권한·모델 응답 품질은 별도 확인한다.

[출처](NOTICE.md) · [라이선스](LICENSE)

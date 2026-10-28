# 실행 연결

이 스킬의 원본은 이 배포 저장소 `.agents/skills/project-wiki/`에 있다. 실행기는 이 저장소의 `scripts/project_wiki.py`다. 다른 저장소에서는 이 실행기의 절대 경로와 `--repo <작업 저장소>`를 사용한다. 설치 시 `--wiki-repo <MCP가 설치된 openNAMU 저장소>`를 반드시 지정한다. 인증 키는 로컬 설정으로만 전달한다.

실행기는 자동 계획에 SKILL.md와 편집 기준만 전달한다. 생애주기의 핵심은 편집 기준에 포함된다. 생애주기·개편·식별자·사용량의 상세 참조는 필요한 수동 작업에서만 읽는다. 사용자가 요청하지 않은 전체 재편은 커밋 훅의 일상 편집에 섞지 않는다.

등록은 명시한 프로젝트에만 적용한다. 프로젝트 식별자는 이후 유지하고, base-ref는 현재 설명의 기준으로 삼을 실제 브랜치를 지정한다.

```sh
python3 scripts/project_wiki.py install --project '개인 개발 위키' \
  --project-id personal-dev-wiki --base-ref main --wiki-repo /path/to/opennamu
```

수동 기록은 UTF-8 입력 파일로 제출한다. 파일에는 저장할 근거만 넣는다. 최초 반환된 기록 ID는 재시도할 때 유지한다.

```sh
python3 scripts/project_wiki.py record --input tmp/wiki-note.txt --id design-note --date 2026-10-21
python3 scripts/project_wiki.py record --input tmp/wiki-note.txt --id design-note --date 2026-10-21 --apply
python3 scripts/project_wiki.py status
python3 scripts/project_wiki.py retry --id manual-design-note
python3 scripts/project_wiki.py pause
python3 scripts/project_wiki.py resume
```

`record`는 기본 미리보기, `--apply`는 저장이다. 커밋 후 실행은 기존 devlog 훅 한 경로를 사용한다. 편집 결과가 `review`이면 사람의 수정이나 근거 부족으로 보류된 항목을 읽는다. 같은 입력을 반복 호출해서 덮어쓰지 않는다.

처음 소개를 만들 때는 사용자 목적·시작 배경·확인한 현재 범위를 입력하고, 기준 브랜치에 존재하는 README·설계 파일을 `--file`로 제공할 수 있다. install은 연결 설정이며 프로젝트 소개를 자동으로 완성하지 않는다. 추적된 근거가 없으면 초기 논의는 일지에 기록하고 개요는 초안으로 남긴다. 기존 개요의 자동 관리 밖 문단을 이어서 고칠 때는 아래 adopt의 지원 범위를 확인한다.

미리보기 입력은 draft 상태라 다음 커밋이 자동 게시하지 않는다. 현재 구현 설명을 수동으로 갱신하려면 `--file <저장소 상대 경로>`로 기준 브랜치에서 읽을 근거 파일을 지정한다. `--topic <기술 이름>`은 관련 문서를 추가 검색한다. 입력의 날짜와 ID는 재시도 때 유지된다.

`pause`는 새 편집 실행을 중지하고 대기 자료와 기존 위키 내용을 보존한다. `resume`은 실행 모드를 복구하며, 대기 항목은 다음 커밋이나 명시적인 retry로 처리한다. 실패한 프로세스의 잠금은 해당 PID가 종료된 것을 확인할 수 있을 때 다음 실행이 회수한다.

설정된 횟수만 자동 재시도한다. 네트워크 복구 후 동일 입력의 저장을 이어갈 때는 retry를 사용한다. `--replan`은 원본 자료가 남아 있을 때 새 문서 상태를 읽어 편집 계획을 다시 만든다. 부분 성공한 기록도 먼저 실제 저장 상태를 확인한다.

위키 본문은 DB에, 스킬과 설계 자료는 Git에, 대기 상태는 Git 관리 폴더에 있다. 이 실행기는 전체 대화 백업이나 여러 PC의 원격 위키 연결을 제공하지 않는다. 연결 요구 사항은 이 배포 저장소의 [README](../../../../README.md)를, 위키 DB 복구는 실제 위키 저장소의 백업 안내를 따른다.

사용량은 실행기가 자동 수집한다. 숫자 조회·갱신·수집 점검을 요청받았을 때만 [사용량 기준](usage-recording.md)의 전용 명령을 사용한다. 수치만 갱신하려고 글쓰기 계획을 재생성하지 않는다.

기존 프로젝트 문단을 계속 갱신하려면 해당 문단을 한 번 명시적으로 등록한다. 원문은 바꾸지 않고 관리 표지만 추가하며 이후 변경 비교의 기준으로 저장한다. 기본 명령은 미리보기이고 `--apply`를 붙여 등록한다. 제목이 중복되거나 기존 자동 문단을 포함하면 보류한다.

```sh
python3 scripts/project_wiki.py adopt --title '프로젝트/개인 개발 위키' --heading '현재 상태' --key current-state
python3 scripts/project_wiki.py adopt --title '프로젝트/개인 개발 위키' --heading '현재 상태' --key current-state --apply
```

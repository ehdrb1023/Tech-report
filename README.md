# 🤖 Discord AI Trend Bot

매일 아침 **9시(KST)**, 지난 24시간 동안 올라온 **AI 에이전트 · 최신 테크 트렌드**를 요약해 Discord로 보내주는 봇입니다.

```
수집(Reddit · X) → 중복보도 제외 → 필터링/스코어링 → arXiv 보강 → Claude 요약 → Discord
```

GitHub Actions의 cron으로 매일 자동 실행되며, 서버가 따로 필요 없습니다.

### 설계 원칙

소스마다 비용 구조가 달라서 전략이 정반대입니다.

| 소스 | 접근 방식 | 전략 |
|---|---|---|
| **Reddit** | 공개 RSS (인증 불필요) | 서브레딧 최신글을 받아 로컬에서 필터링 |
| **X** | 공식 API(종량제) 또는 Nitter 미러 | **특정 인물**의 타임라인만. 검색 안 씀 |
| **arXiv** | 공개 API (무료) | 수집 소스가 아님. 위 두 곳에서 **링크된 논문만** 조회 |

**arXiv는 발견이 아니라 보강 역할입니다.** 하루에 올라오는 논문 수백 편을 다 훑는 대신, 사람들이 실제로 링크한 논문만 골라 초록·저자를 채웁니다. 이미 커뮤니티 필터를 통과한 논문만 리포트에 오르고, 아무도 안 읽은 초록이 리포트를 채우지 않습니다.

> 💡 키워드는 **받아올 것을 정하지 않습니다.** Reddit·X 어느 쪽에도 검색어를 보내지 않고, 받아온 뒤 "무엇을 리포트에 올릴지" 순위를 매기는 데만 씁니다.

---

## 목차

1. [리포트 예시](#리포트-예시)
2. [빠른 시작](#빠른-시작)
3. [API 키 발급](#api-키-발급)
4. [GitHub Actions 설정](#github-actions-설정)
5. [설정 커스터마이징](#설정-커스터마이징)
6. [CLI 사용법](#cli-사용법)
7. [프로젝트 구조](#프로젝트-구조)
8. [동작 방식](#동작-방식)
9. [문제 해결](#문제-해결)

---

## 리포트 예시

Discord에 이런 형태로 전송됩니다.

> **📰 AI · 테크 트렌드 브리핑 — 2026년 08월 03일**
>
> **오픈웨이트 모델 경쟁이 하루 사이에 두 건 터졌다**
> • Qwen 3.8 Max와 MiniMax-H3가 몇 시간 간격으로 공개
> • DeepMind의 SkillSmith, 모델 가중치를 '모달리티'로 취급하는 새 접근
> • 에이전트 시스템 검증 벤치마크 논문이 연달아 등장
>
> 🏷️ `agentic` · `open-weight` · `tool use` · `benchmark`
>
> **🤖 AI 에이전트**
> 𝕏 **[Codex 스킬로 고객 피드백을 로드맵으로](https://x.com/...)**
> 고객 피드백 분석을 로드맵 초안까지 자동화하는 워크플로 공유.
> ➡️ 사내 피드백 파이프라인에 그대로 옮겨볼 만한 패턴.
> -# @gdb

---

## 빠른 시작

### 1. 저장소 준비

```bash
git clone <your-repo-url>
cd discord-ai-trend-bot
```

### 2. Python 환경

Python **3.11 이상**이 필요합니다 (`zoneinfo`, 최신 타입 문법 사용).

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. 환경변수 설정

```bash
cp .env.example .env
```

`.env`를 열어 값을 채웁니다. **`.env`는 `.gitignore`에 포함되어 커밋되지 않습니다.**

```dotenv
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-opus-5

# 선택(강력 권장) — 아래 "Reddit" 항목 참고
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=discord-ai-trend-bot/1.0 (by /u/your_id)
```

> ⚠️ 실제 키/웹훅 URL은 **절대 `.env.example`에 넣지 마세요.** 이 파일은 `.gitignore`에서 예외 처리(`!.env.example`)되어 있어 그대로 커밋됩니다.

### 4. 동작 확인

Discord로 보내지 않고 콘솔에서 결과만 확인:

```bash
python -m src.main --dry-run
```

실제 전송:

```bash
python -m src.main
```

---

## API 키 발급

### Discord Webhook (필수)

1. 리포트를 받을 Discord 채널 → **채널 편집(⚙️)**
2. **연동(Integrations)** → **웹후크(Webhooks)** → **새 웹후크**
3. 이름을 정하고 **웹후크 URL 복사**
4. `.env`의 `DISCORD_WEBHOOK_URL`에 붙여넣기

> 웹훅 URL은 **비밀번호와 같습니다.** 유출되면 누구나 해당 채널에 글을 쓸 수 있으니 공개 저장소에 커밋하지 마세요. 실수로 노출됐다면 Discord에서 웹훅을 삭제하고 새로 만드세요.

### Anthropic API (필수)

1. <https://console.anthropic.com/settings/keys> 접속
2. **Create Key** → 키 복사
3. `.env`의 `ANTHROPIC_API_KEY`에 붙여넣기

기본 모델은 `claude-opus-5`입니다. 비용을 줄이려면 `CLAUDE_MODEL=claude-sonnet-5`로 바꾸세요.
하루 1회, 25건 요약 기준으로 호출량은 매우 적습니다(입력 약 2만 토큰 내외).

### Reddit — 키 불필요 (공개 RSS)

인증 없이 동작합니다. `.env`에서 **User-Agent만** 본인 계정명으로 바꾸면 됩니다.

```dotenv
REDDIT_USER_AGENT=python:discord-ai-trend-bot:v1.0 (by /u/your_reddit_id)
```

Reddit 규약 형식(`<platform>:<app id>:<version> (by /u/<username>)`)이며, 익명 UA보다 차단 위험이 낮습니다.

#### 왜 JSON이 아니라 RSS인가

공개 `.json` 엔드포인트는 **어떤 User-Agent로도 403**입니다. 실측했습니다.

| 방식 | 결과 |
|---|---|
| `.json` + 봇 UA / 규약형 UA / 브라우저 UA | 403 (전부) |
| `old.reddit.com/.json` | 403 |
| **`.rss`** | **200** ✅ |
| `.rss` + `after=` 페이지네이션 | 403 (미지원) |

그래서 RSS를 씁니다. 대신 제약이 있습니다.

- **업보트·댓글 수가 없습니다** → 인기도 점수가 0이 되고 `min_upvotes`가 무효
- **페이지네이션이 안 됩니다** → 서브레딧당 최신 **~25건**이 한계
- **429가 자주 뜹니다** → 요청 간 딜레이 + 재시도 필요

#### 실행 시간

서브레딧 10개 기준 **5~7분** 걸립니다. 429를 맞으면 45초 쉬었다 재시도하기 때문입니다. 일 1회 실행이라 문제되지 않지만, 워크플로 타임아웃을 30분으로 잡아뒀습니다.

실측(같은 IP, 딜레이만 변경):

| 딜레이 | 성공 서브레딧 | 수집 | 소요 |
|---:|---:|---:|---:|
| 2초 | 6/10 | 94건 | 266초 |
| **5초 (기본)** | **8/10** | **108건** | 417초 |
| 10초 | 7/10 | 105건 | 370초 |

딜레이를 늘려도 429가 크게 줄지 않았고, **실제로 서브레딧을 구제한 건 재시도**였습니다. 매 실행마다 2~3개 서브레딧이 유실되지만 어느 게 실패할지는 매번 달라서 며칠 누적하면 고르게 커버됩니다.

#### 선택: OAuth를 넣으면

앱 등록이 가능해지면 `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET`를 채우세요. **코드 수정 없이** 자동으로 JSON API 경로로 바뀌고 업보트 수와 페이지네이션이 살아납니다.

1. <https://www.reddit.com/prefs/apps> → **create another app...** → **script** 선택
2. `redirect uri`에 `http://localhost:8080` (사용 안 되지만 필수 입력)
3. 앱 이름 아래 문자열 → `REDDIT_CLIENT_ID`, `secret` → `REDDIT_CLIENT_SECRET`

### X — 키 없이도 동작, 넣으면 공식 API로 전환

`X_BEARER_TOKEN`을 **비워두면** Nitter/RSSHub 미러 RSS를 씁니다. 무료지만 서드파티 미러라 가용성이 보장되지 않고, 좋아요·리트윗 수를 못 받아 스코어링이 부정확합니다.

토큰을 넣으면 **코드 수정 없이** 공식 API(user timeline)로 전환됩니다.

#### 공식 API 비용

X는 2026년 2월부터 **종량제**입니다. 무료 티어와 Basic($200/월) 모두 폐지됐고, 읽기 1건당 과금됩니다.

| 항목 | 값 |
|---|---|
| 읽기 단가 | **$0.005 / 건** |
| 실측 수집량 (계정 18명, 24시간) | 약 26건 |
| 월 읽기 건수 | 약 800~3,000건 |
| **예상 월 비용** | **약 $4~15** |

비용은 **`계정 수 × 그 사람의 포스팅 량`**으로 결정됩니다. 키워드로는 1건도 줄지 않습니다 — 줄이려면 `config.yaml`의 `x.accounts`를 줄여야 합니다.

코드는 받는 양을 최소화하도록 짜여 있습니다.

- `start_time`으로 24시간 창을 **서버에 넘김** (받은 뒤 자르지 않음)
- `exclude=retweets,replies`
- `max_results_per_account`로 계정당 상한 → 누가 하루에 200개를 써도 여기까지만 과금
- 실행할 때마다 읽기 건수와 예상 비용을 로그에 출력

> ⚠️ 위 단가는 서드파티 정리글 기준입니다. 결제 전 [developer.x.com](https://developer.x.com)에서 직접 확인하세요.

#### 검색 API를 안 쓰는 이유

`/2/tweets/search/recent`를 쓰면 서버에서 키워드 필터가 되지만, 읽기 단가는 같은데 잡히는 양이 자릿수가 다릅니다.

| 방식 | 하루 읽기 | 월 비용 | 신호 품질 |
|---|---|---|---|
| 계정 타임라인 (현재) | 수십 건 | $4~15 | 높음 — 검증된 인물 |
| 키워드 검색 | 수천 건 | $수백 | 낮음 — 봇·스팸 섞임 |

---

## GitHub Actions 설정

### 1. Secrets 등록

저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| 이름 | 필수 | 설명 |
|---|:--:|---|
| `DISCORD_WEBHOOK_URL` | ✅ | Discord 웹훅 URL |
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API 키 |
| `REDDIT_CLIENT_ID` | — | 있으면 OAuth+JSON, 없으면 공개 RSS |
| `REDDIT_CLIENT_SECRET` | — | 위와 동일 |
| `X_BEARER_TOKEN` | — | 있으면 X 공식 API, 없으면 Nitter 미러 |

Variables에 `REDDIT_USER_AGENT`를 본인 계정명으로 넣어두세요.

### 2. Variables 등록 (선택)

같은 화면의 **Variables** 탭:

| 이름 | 예시 |
|---|---|
| `CLAUDE_MODEL` | `claude-opus-5` |
| `REDDIT_USER_AGENT` | `discord-ai-trend-bot/1.0 (by /u/your_id)` |

### 3. 실행 확인

`.github/workflows/daily-report.yml`이 이미 포함되어 있습니다.

- **자동**: 매일 `22:45 UTC` = **07:45 KST 트리거** → 약 08:00 도착
- **수동**: 저장소 → **Actions** 탭 → *Daily AI Trend Report* → **Run workflow**
  - `dry_run`을 켜면 Discord로 보내지 않고 로그에만 출력합니다 (설정 검증용).

> ⏰ GitHub Actions의 cron은 러너 혼잡도에 따라 **5~15분 늦게** 시작될 수 있고, 파이프라인 자체가 **약 8분** 걸립니다(Reddit 429 재시도). 그래서 도착 목표 시각보다 15분 앞당겨 트리거합니다.

> 💡 **Vercel은 이 작업에 맞지 않습니다.** 서버리스 함수 실행 한도가 Hobby 60초 / Pro 300초인데 우리 파이프라인은 8분이 걸려 타임아웃됩니다. 상태 파일(`state/seen.json`)도 외부 스토리지가 필요해집니다. GitHub Actions는 30분 타임아웃에 `actions/cache`로 상태까지 처리되고 무료입니다.

> 💤 공개 저장소는 **60일간 커밋이 없으면 스케줄이 자동 비활성화**됩니다. 이메일 알림이 오면 Actions 탭에서 다시 활성화하세요.

### 시간대 변경

`0 0 * * *`는 UTC 기준입니다. 원하는 현지 시각에서 UTC 오프셋을 빼세요.

| 현지 시각 | 시간대 | cron |
|---|---|---|
| 09:00 | KST (UTC+9) | `0 0 * * *` |
| 08:00 | KST | `0 23 * * *` |
| 09:00 | JST (UTC+9) | `0 0 * * *` |
| 09:00 | PST (UTC-8) | `0 17 * * *` |

---

## 설정 커스터마이징

비밀값은 `.env`, 그 외 모든 설정은 **`config.yaml`**에 있습니다.

### 수집 대상 바꾸기

```yaml
reddit:
  subreddits:
    - LocalLLaMA
    - MachineLearning
    - AI_Agents        # 무료라 늘려도 비용 부담 없음
  max_pages_per_sub: 5 # 1페이지 = 100건. 24시간을 못 덮으면 경고가 뜸

x:
  accounts:
    - karpathy         # @ 없이 계정명만
    - sama             # 공식 API 사용 시 이 목록 길이 = 월 비용
  max_results_per_account: 20   # 계정당 상한 = 비용 상한
```

arXiv에는 `categories` 설정이 없습니다 — 카테고리를 훑지 않고 링크된 논문만 조회하기 때문입니다.

### 키워드 / 스코어링 조정

```yaml
scoring:
  keywords:
    high:            # 가중치 3.0 — 꼭 보고 싶은 주제
      weight: 3.0
      terms: [ai agent, agentic, mcp, ...]
    blocked:         # 매칭되면 즉시 폐기
      - nsfw
      - who's hiring
```

최종 점수 = `키워드 가중치 합(상위 5개) + 인기도(로그 스케일) + 최신성 + 소스 가중치 + 논문 가산점 + 교차언급 가산점`

**실질적인 관문은 `min_keyword_score`입니다.** 총점은 최신성·소스 가중치만으로도 쉽게 올라가서 내용 필터 역할을 못 합니다. 그래서 "키워드 점수" 자체에 문턱을 둡니다.

```yaml
collect:
  min_keyword_score: 2.0   # ← 진짜 관문
  max_items: 0             # 0 = 무제한. 문턱 통과분을 전부 Claude에게
  per_source_cap: 0
```

실측(X, 72시간, 81건 수집) 기준 문턱별 통과량입니다.

| 문턱 | 통과 | 의미 | 판정 |
|---:|---:|---|---|
| 1.0 | 36건 | `release`·`api`·`research` 같은 일반어 1개 | 잡담이 섞임 |
| **2.0** | **30건** | `claude`·`qwen`·`deepseek` 등 1개 이상 | **권장** |
| 3.0 | 17건 | high 1개 또는 medium+low | "Qwen 3.8 Max 공개" 같은 릴리스가 탈락 |

개수 상한을 두지 않는 이유는, 24시간 안에 나오는 유용한 글이 그리 많지 않고 **산수로 미리 자르는 것보다 Claude가 내용을 읽고 고르는 편이 낫기** 때문입니다. 문턱에서 잘린 건 Claude가 되살릴 수 없습니다.

| 증상 | 조정 |
|---|---|
| 관련 없는 글이 섞임 | `collect.min_keyword_score` ↑ |
| 볼 만한 게 빠짐 | `min_keyword_score` ↓ 또는 키워드 목록 보강 |
| 리포트가 너무 김 | `summarizer.py`의 "전체 최대 15건" 문구 조정 |
| Reddit 잡담이 많음 | `reddit.min_upvotes` ↑ |
| 논문이 너무 자주/드물게 올라옴 | `scoring.paper_bonus` 조정 |

> 이 숫자들(가중치 3.0/2.0/1.0, 상한 4.0, 문턱 2.0 등)은 소규모 표본으로 잡은 출발점입니다. 실제 리포트를 며칠 보면서 조정하세요.

### 저장공간 — 글은 디스크에 남지 않습니다

수집한 글은 **메모리에만** 존재하고 프로세스가 끝나면 사라집니다. 제목·본문이 디스크에 닿는 경로는 없습니다.

디스크에 쓰는 건 두 개뿐이고, 둘 다 **ID만** 저장합니다.

| 파일 | 내용 | 크기 |
|---|---|---|
| `state/seen.json` | 보도한 글 ID + arXiv ID + 타임스탬프 | 하루치 약 600 bytes |
| `state/x_user_ids.json` | 계정명 → 숫자 ID 캐시 | 수백 bytes |

7일 TTL로 오래된 항목이 자동 만료되어 파일이 무한히 커지지 않습니다(약 15KB 상한).

### 중복 보도 방지

화제의 논문은 며칠 회자되므로 24시간 창만으로는 반복 보도를 못 막습니다. 보도한 글 ID와 arXiv ID를 `state/seen.json`에 기록해 두고, 재등장하면 제외합니다.

```yaml
state:
  enabled: true
  ttl_days: 7      # 이 기간이 지나면 만료되어 다시 보도 가능
```

GitHub Actions에서는 `actions/cache`로 실행 간에 유지됩니다. 캐시가 없어도(첫 실행, 만료) 중복 억제만 안 될 뿐 동작에는 지장이 없습니다. 테스트할 땐 `--no-state`로 무시할 수 있습니다.

---

## CLI 사용법

```bash
python -m src.main [옵션]
```

| 옵션 | 설명 |
|---|---|
| `--dry-run` | Discord로 보내지 않고 콘솔에 출력 |
| `--dump-prompt [PATH]` | **Claude 호출 직전까지만** 실행하고 요청 내용을 출력 (API 키 불필요, 과금 없음) |
| `--no-llm` | Claude 요약 없이 원본 링크만 (수집기 점검용) |
| `--no-state` | 중복 보도 기록을 무시 (테스트용) |
| `--hours N` | 수집 기간 변경 (기본 24) |
| `--max-items N` | 리포트 최대 아이템 수 |
| `--source NAME` | 특정 소스만 (`reddit`/`x`, 반복 지정 가능) |
| `--config PATH` | 다른 설정 파일 사용 |
| `-v, --verbose` | 디버그 로그 |

```bash
# X만, 최근 48시간, 요약 없이 — 미러 상태 점검
python -m src.main --source x --hours 48 --no-llm --dry-run -v

# 전송 없이 실제 Claude 요약 결과만 확인
python -m src.main --dry-run

# 같은 데이터로 반복 테스트 (중복 억제 무시)
python -m src.main --dry-run --no-state

# Claude에 뭐가 들어가는지 호출 없이 확인 (키 없이도 동작)
python -m src.main --dump-prompt
python -m src.main --dump-prompt out.txt --hours 72
```

**종료 코드**: `0` 성공 / `1` 전송 또는 파이프라인 실패 / `2` 설정 오류

---

## 프로젝트 구조

```
discord-ai-trend-bot/
├── .github/workflows/daily-report.yml   # 매일 09:00 KST cron + state 캐시
├── config.yaml                          # 수집 대상 · 키워드 · 스코어링
├── .env.example                         # 환경변수 템플릿 (실제 키 금지)
├── requirements.txt
├── state/                               # 중복 보도 기록 (gitignore)
└── src/
    ├── main.py            # 진입점 · CLI · 파이프라인 조립
    ├── config.py          # config.yaml + .env 로딩
    ├── models.py          # Item / Paper / Report + arXiv 링크 추출
    ├── scoring.py         # 필터링 · 스코어링 · 논문 교차언급 병합
    ├── enrichment.py      # arXiv 보강 (수집이 아님)
    ├── state.py           # 중복 보도 방지 기록
    ├── summarizer.py      # Claude API 요약 (구조화 출력)
    ├── discord_sender.py  # Webhook 전송 · 임베드 분할
    └── collectors/
        ├── base.py        # 공통 HTTP · 재시도 · 예외 격리
        ├── reddit.py      # OAuth + 24시간 경계까지 페이지네이션
        └── x.py           # 공식 API ↔ Nitter 미러 자동 전환
```

---

## 동작 방식

### 1. 수집

두 수집기가 **병렬로** 실행됩니다. 한 소스가 실패해도 나머지로 리포트가 나가도록 각 수집기는 예외를 밖으로 던지지 않습니다.

- **Reddit** — 공개 RSS(`/r/{sub}/new/.rss`). 요청 사이 딜레이를 두고, 429를 맞으면 쉬었다 재시도합니다. 자격증명이 있으면 OAuth+JSON 경로로 자동 전환되어 업보트와 페이지네이션이 살아납니다.
- **X** — 토큰이 있으면 공식 user timeline API(`start_time`·`exclude`·`max_results`로 받는 양을 서버에서 제한), 없으면 미러 RSS를 순차 시도합니다. 한 번 성공한 미러를 기억해 다음 계정부터 우선 쓰고, `200 OK`로 안내문만 돌려주는 가짜 피드(`"RSS reader not yet whitelisted!"` 등)는 걸러냅니다.

### 2. 중복 보도 제외

`state/seen.json`에 기록된 글 ID·arXiv ID를 제외합니다. 같은 논문이 며칠 연속 올라가는 걸 막습니다.

### 3. 필터링 · 스코어링

중복 제거(ID + 제목 정규화 → 크로스포스트 제거) → **같은 논문을 가리키는 글들을 병합**(인게이지먼트가 가장 높은 글을 대표로, 나머지는 `also_from`에 누적하고 점수 가산) → 차단 키워드 폐기 → 점수 계산 → 임계값 필터 → 소스별 상한 → 상위 N개 선별.

### 4. arXiv 보강

**선별을 통과한 글이 링크한 논문에 대해서만** arXiv API를 호출합니다(`id_list`로 배치 조회). 초록·저자·정확한 제목이 붙어야 Claude가 "누가 왜 언급했는가 + 논문이 실제로 뭔가"를 함께 쓸 수 있습니다.

논문 식별은 **URL 정규식만** 씁니다. 실측 결과 "논문 제목만 언급"하는 경우는 거의 없고, `research` 같은 단어로 추측하면 오탐이 대부분이었습니다(예: "chatgpt로 research 한다"). 조회에 실패해도 링크 자체는 살려둡니다.

### 5. 요약

Claude API의 **구조화 출력**(`output_config.format` + JSON Schema)으로 스키마가 보장된 JSON을 받습니다.

- 모델이 만들어낸 **환각 URL은 입력에 없으면 제거**합니다.
- API 호출 실패 / 파싱 실패 / `stop_reason: refusal` 시 **원본 링크 목록으로 폴백**해 리포트는 반드시 나갑니다.
- 논문이 0편인 날은 "화제의 논문" 섹션을 만들지 않도록 프롬프트에서 지시합니다.

### 6. 전송

Discord 제한(임베드 description 4096자, 메시지당 임베드 10개·합계 6000자)에 맞춰 자동 분할하고, `429` 응답 시 `Retry-After`만큼 대기 후 재시도합니다. 파이프라인이 통째로 실패하면 실패 사실을 Discord로 알립니다.

---

## 문제 해결

### Reddit에서 429가 자주 뜹니다

**정상입니다.** Reddit은 비인증 RSS 요청을 강하게 제한합니다. 코드가 45초 쉬었다 재시도하므로 대부분 구제되고, 로그에 `재시도 후에도 429 — 건너뜀`이 보이는 서브레딧만 그 회차에서 유실됩니다.

매 실행 2~3개가 빠지는 건 예상 범위입니다. 어느 게 실패할지는 매번 달라서 며칠이면 고르게 커버됩니다. 더 줄이고 싶으면:

- `config.yaml`의 `reddit.subreddits` 개수를 줄이거나
- `rate_limit_wait_seconds`를 늘리거나
- OAuth 자격증명을 넣으세요 (JSON 경로는 한도가 훨씬 넉넉합니다)

### Reddit 글이 24시간을 다 못 덮습니다

`RSS 상한(~25건)으로 24시간을 다 못 덮었습니다` 경고가 뜨면, 그 서브레딧은 하루 25건 넘게 올라오는 곳입니다. **RSS에는 페이지네이션이 없어서 이게 한계입니다.** 더 받으려면 OAuth 자격증명이 필요합니다.

### X가 0건으로 나옵니다

로그를 확인하세요.

- `미러 응답 0/18 계정` → **미러가 전부 죽었습니다.** `config.yaml`의 `x.rss_templates`를 살아 있는 인스턴스로 교체하세요. 목록은 <https://status.d420.de/> 에서 확인할 수 있습니다.
- `미러 응답 18/18 계정, 최근 글 있는 계정 0개` → 미러는 정상이고 그냥 24시간 내 새 글이 없는 것입니다. `--hours 48`로 확인해 보세요.

미러 상태를 직접 점검하려면:

```bash
python -m src.main --source x --no-llm --dry-run -v
```

> Nitter/RSSHub 미러는 서드파티 운영이라 가용성이 보장되지 않습니다. X 데이터가 중요하다면 X API 유료 플랜을 검토하세요.

### 논문이 0편으로 나옵니다

`[arxiv] 링크된 논문 없음 — 보강 건너뜀`은 **정상 동작**입니다. 24시간 안에 아무도 arXiv 링크를 안 올린 것뿐입니다.

실측 기준 X만으로는 **하루 1~2편** 수준이라, 논문 섹션이 비는 날이 흔합니다. 화제가 된 논문만 올리기로 한 설계의 결과입니다. 논문을 더 보고 싶다면:

- **Reddit OAuth를 설정하세요** — r/MachineLearning, r/LocalLLaMA는 논문 링크 공유가 활발해 대부분의 논문이 여기서 나옵니다.
- `--hours 48`로 기간을 늘려 확인해 보세요.

Reddit이 막힌 상태라면 X만 남아 논문이 거의 안 잡히는 게 당연합니다.

### Discord로 아무것도 오지 않습니다

1. 웹훅 URL이 유효한지 확인:
   ```bash
   curl -X POST -H "Content-Type: application/json" \
        -d '{"content":"test"}' "$DISCORD_WEBHOOK_URL"
   ```
2. 로그에 `Discord 404`가 보이면 웹훅이 삭제된 것입니다 — 새로 만드세요.
3. `Discord 401/403`이면 URL이 잘못됐습니다.

### Actions는 성공인데 메시지가 없습니다

`--dry-run`으로 실행됐거나 선별된 항목이 0건일 수 있습니다. Actions 로그에서 `수집 N건 → ... → 선정 N건` 줄을 확인하세요. 0건이면 `config.yaml`의 `collect.min_score`를 낮춰 보세요.

### Windows에서 이모지가 깨집니다

콘솔 인코딩(cp949) 문제입니다. 코드가 stdout을 UTF-8로 재설정하지만, 그래도 깨진다면:

```powershell
chcp 65001
$env:PYTHONIOENCODING="utf-8"
```

### `400 invalid_request_error`가 납니다

`CLAUDE_MODEL`을 오래된 모델로 바꿨는지 확인하세요. 이 프로젝트는 최신 모델용으로 작성되어 있습니다(assistant prefill 미사용, `temperature` 미전달). 지원 모델 ID는 `claude-opus-5`, `claude-sonnet-5` 등입니다.

---

## 라이선스

MIT

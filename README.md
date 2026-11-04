# ERPBench

A benchmark for computer-use agents on enterprise ERP software. Agents see only
screenshots of a live, self-hosted ERPNext v15 instance and act through mouse
and keyboard actions. Grading reads the resulting records back from the ERPNext
database, so a run only counts as a success if the data is actually correct.

Paper: *ERPBench: A State-Grounded Evaluation Paradigm for Computer-Use Agents in Enterprise Software*,
[arXiv:2609.17885](https://arxiv.org/abs/2609.17885).

## Layout

```
ERPBench/
├── tasks/            task definitions (T1 single field, T2 multi field, T3 multi screen, warm-up)
├── runner.py         runs tasks against the stack and grades each run
├── evaluation/
│   ├── graders/      DB grader, stage (partial) grader, log metrics
│   ├── reset/        per-run unique names, fixtures, teardown
│   ├── reports/      scoreboard.py: per-run summary
│   ├── erpnext_client.py
│   └── auto_approver.py     approves every proposed action
├── stack/            runs inside the observer container
│   ├── observer_server.py   evaluation runtime: action execution + audit logs
│   ├── inspector.py         screenshot + mouse/keyboard executor
│   ├── *_brain.py           agent adapters (Claude, Qwen3-VL, OpenCUA, UI-TARS, Holo3)
│   ├── system_prompt_*.md   agent prompts (UI-TARS's prompt is built in uitars_brain.py)
│   └── human_capture.py, templates/human.html   human-baseline capture
├── results/          run outputs written by runner.py
├── docker-compose.yml, Dockerfile, supervisord.conf
└── .env.example
```

## Requirements

- Docker with Compose v2 (Docker Desktop or Colima on macOS). Give the VM at
  least 4 CPUs and 8 GB of memory.
- Python 3.12 on the host, for the runner and graders.
- An API key, and for open models an OpenAI-compatible endpoint, for the agent
  you want to evaluate.

## Setup

```bash
git clone https://github.com/Accenture/ERPBench.git
cd ERPBench
cp .env.example .env              # fill in the keys for your agent
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d --build      # first boot provisions ERPNext, about 10 minutes
docker compose logs -f erpnext-create-site
```

Then open http://localhost:8080, log in as `Administrator` / `admin`, and
complete the ERPNext setup wizard. Tasks rely on the default master data the
wizard creates (customer groups, price lists, territories).

You do not need to log in the agent's own browser (the Chromium inside the
observer container, visible on noVNC). Before each task the runner checks it
and, if it is on the login page, signs it in with `ERPNEXT_BROWSER_USR` /
`ERPNEXT_BROWSER_PWD` (default `Administrator` / `admin`). A failed login is
logged by the observer as `ERPNext login ... failed`.

| URL | What |
|---|---|
| http://localhost:8080 | ERPNext |
| http://localhost:3002 | live view of the agent's screen (noVNC) |
| http://localhost:5052/status | observer state |

## Running tasks

The agent is selected by `BRAIN_PROVIDER` in `.env` (`claude`, `qwen`,
`opencua`, `uitars`, `holo3`). After changing it, run `docker compose up -d` so
the container picks it up. `--brain` on the runner is only a label for the
results folder, so keep it consistent with `BRAIN_PROVIDER`.

```bash
# smoke test: one task, one run
python runner.py --task tasks/t1_single_field/change_industry.yaml --brain claude --runs 1

# full tiers, 5 runs per task
python runner.py --suite t1 --brain claude --runs 5
python runner.py --suite t1,t2,t3 --brain claude --runs 5
```

Each invocation writes `results/RUN-<brain>-<id>/` with one JSON file per task
run (`per_task/`) and the raw action and turn logs (`raw_logs/`). Other
options: `--timeout-scale`, `--start-from <task>` (resume), `--run-id`.

## Scoring

```bash
python evaluation/reports/scoreboard.py results/<run-id>/
```

Each task run records:

- `success`: every DB grader passed (binary task success)
- `partial_stages`: navigation, interaction, commit and database stages
- `log_metrics`: action counts and per-call token usage
- `started_ts` / `ended_ts`: wall-clock duration as reported in the paper is
  `ended_ts - started_ts`

## Human baseline

Set `BRAIN_PROVIDER=human` and run `docker compose up -d`. Open
http://localhost:5052/human, which shows the task and the live ERPNext screen,
then start the runner:

```bash
python runner.py --brain human --run-id RUN-human-<name> --suite t1,t2,t3 --runs 5
```

Clicks and typing in the browser are captured into the same log format the
agents produce, so the same graders apply.

## Citation

If you use ERPBench, please cite:

```bibtex
@article{bhagtani2026erpbench,
  title   = {ERPBench: A State-Grounded Evaluation Paradigm for Computer-Use Agents in Enterprise Software},
  author  = {Bhagtani, Kratika and Sridhar, Kusha and Pouyan, Maziyar Baran and Zhao, Yuying and Siow, Eugene},
  journal = {arXiv preprint arXiv:2609.17885},
  year    = {2026}
}
```

## License

ERPBench is released under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for attribution.

ERPNext and the Frappe Framework, which the benchmark environment pulls as a
Docker image, are licensed separately (ERPNext under the GNU GPL v3.0, Frappe
under the MIT License) and are not included in this repository.

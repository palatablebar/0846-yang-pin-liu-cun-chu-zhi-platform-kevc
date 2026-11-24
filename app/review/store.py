import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,
              request_key TEXT UNIQUE NOT NULL,
              created_at TEXT NOT NULL,
              profile TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              payload TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              frozen_review TEXT
            );
            CREATE TABLE IF NOT EXISTS legal_holds(
              id TEXT PRIMARY KEY,
              sample_id TEXT NOT NULL,
              effective_on TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(sample_id, effective_on)
            );
            CREATE TABLE IF NOT EXISTS deactivations(
              id TEXT PRIMARY KEY,
              run_id TEXT NOT NULL,
              hold_id TEXT NOT NULL,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(run_id, hold_id)
            );
            ''')
            # 旧库补列：frozen_review 保存停用前的签发快照，供审计还原原证据。
            columns = {row["name"] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
            if "frozen_review" not in columns:
                db.execute("ALTER TABLE runs ADD COLUMN frozen_review TEXT")

    def find_by_request(self, request_key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
        return dict(row) if row else None

    def create(self, request_key, profile, payload):
        existing = self.find_by_request(request_key)
        if existing:
            # 同一来源重复执行得到同一批次；补录保全后重提要刷新结论，
            # 但批次身份、状态与版本号保持不变，不能放出漏算保全的旧结论。
            with self.connect() as db:
                db.execute("UPDATE runs SET payload=? WHERE id=?",
                           (json.dumps(payload, ensure_ascii=False), existing["id"]))
            return self.get(existing["id"])
        run_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO runs(id,request_key,created_at,profile,status,revision,payload) VALUES(?,?,?,?,?,?,?)",
                       (run_id, request_key, datetime.now(timezone.utc).isoformat(), profile, "DRAFT", 0,
                        json.dumps(payload, ensure_ascii=False)))
        return self.get(run_id)

    def get(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def review(self, run_id, decision, comment, expected_revision):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None, "missing"
            if row["revision"] != expected_revision:
                return None, "stale"
            db.execute("UPDATE runs SET status=?, revision=revision+1, comment=? WHERE id=?",
                       (decision, comment, run_id))
        return self.get(run_id), None

    # ---- 迟到保全补录 -------------------------------------------------

    def list_holds(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM legal_holds ORDER BY created_at, sample_id").fetchall()
        return [dict(row) for row in rows]

    def holds_effective_on(self, day):
        """补录保全按样品生效日在原判定日（含）前生效即纳入计算。"""
        day_text = day.isoformat() if isinstance(day, date) else str(day)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM legal_holds WHERE effective_on <= ? ORDER BY sample_id",
                (day_text,),
            ).fetchall()
        return [dict(row) for row in rows]

    def find_hold(self, sample_id, effective_on):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM legal_holds WHERE sample_id=? AND effective_on=?",
                (sample_id, effective_on),
            ).fetchone()
        return dict(row) if row else None

    def add_hold(self, sample_id, effective_on, note):
        """登记补录保全；同一（样品、生效日）重试返回既有记录，不重复登记。"""
        existing = self.find_hold(sample_id, effective_on)
        if existing:
            return existing, False
        hold_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute(
                "INSERT INTO legal_holds(id,sample_id,effective_on,note,created_at) VALUES(?,?,?,?,?)",
                (hold_id, sample_id, effective_on, note, datetime.now(timezone.utc).isoformat()),
            )
        return self.find_hold(sample_id, effective_on), True

    def list_reviewable_runs(self):
        """已签发或已被停用的批次，用于补录时核对历史签发。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE status IN ('APPROVED','DEACTIVATED') ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def deactivation_exists(self, run_id, hold_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM deactivations WHERE run_id=? AND hold_id=?", (run_id, hold_id),
            ).fetchone()
        return row is not None

    def deactivate_run(self, run_id, hold_id, reason, frozen_review=None):
        """登记停用并把签发状态改为 DEACTIVATED；同一更正重试不重复登记。

        frozen_review 保存停用瞬间的签发状态快照，保证审计证据与原签发一致。
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM deactivations WHERE run_id=? AND hold_id=?", (run_id, hold_id),
            ).fetchone()
            if existing:
                return False
            db.execute(
                "INSERT INTO deactivations(id,run_id,hold_id,reason,created_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex[:12], run_id, hold_id, reason, datetime.now(timezone.utc).isoformat()),
            )
            db.execute(
                "UPDATE runs SET status='DEACTIVATED', revision=revision+1, frozen_review=? "
                "WHERE id=? AND status='APPROVED'",
                (json.dumps(frozen_review, ensure_ascii=False) if frozen_review is not None else None, run_id),
            )
        return True

    def list_deactivations(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT d.*, h.sample_id, h.effective_on FROM deactivations d "
                "JOIN legal_holds h ON h.id = d.hold_id ORDER BY d.created_at"
            ).fetchall()
        return [dict(row) for row in rows]

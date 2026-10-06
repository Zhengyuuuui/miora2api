-- miora2api schema
-- 约定：accounts 表结构对齐 exa_pool.db（2026-09-27 Exa AI session）以便统一管理

CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,          -- miora userId
    label        TEXT NOT NULL DEFAULT '',
    auth_token   TEXT NOT NULL,             -- RS256 JWT (iss=ardot.ai, 15d)
    refresh_token TEXT,                     -- opaque 814 chars
    enabled      INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'unknown',  -- unknown|active|cooldown|disabled|expired
    last_error   TEXT,
    cooldown_until REAL NOT NULL DEFAULT 0,
    requests     INTEGER NOT NULL DEFAULT 0,
    successes    INTEGER NOT NULL DEFAULT 0,
    failures     INTEGER NOT NULL DEFAULT 0,
    credits      REAL,                      -- 剩余额度
    credit_total REAL,
    plan         TEXT,                      -- Free / Pro
    country      TEXT,
    weight       INTEGER NOT NULL DEFAULT 1,
    last_used    REAL NOT NULL DEFAULT 0,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 统一的生成任务表：图像 / 视频 / 3D / 音频 都进这里
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,        -- miora taskId
    account_id     TEXT,
    media_type     TEXT NOT NULL,           -- image|video|3d|audio
    model          TEXT NOT NULL,           -- modelId e.g. gem-3.1
    task_kind      TEXT,                    -- text_to_image / image_to_video / image_to_3d ...
    prompt         TEXT,
    status         TEXT NOT NULL,           -- pending|processing|completed|failed|cancelled
    queue_state    TEXT,                    -- WAITING_QUEUE_WORKER ...
    queue_position INTEGER,
    est_seconds    INTEGER,
    progress       INTEGER,
    file_key       TEXT,                    -- 原始产物路径
    -- ★ 签名 URL（3600s 过期）与其签名时刻，便于判断是否需刷新
    signed_url     TEXT,
    url_signed_at  REAL,
    url_expires_at REAL,
    resolution     TEXT,
    aspect_ratio   TEXT,
    ref_count      INTEGER DEFAULT 0,
    credits_used   REAL,
    error          TEXT,
    raw            TEXT,                    -- 原始响应 JSON
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    account_id  TEXT,
    media_type  TEXT NOT NULL,
    model       TEXT,
    file_key    TEXT,
    file_ext    TEXT,                       -- jpg / mp4 / glb ...
    bytes       INTEGER,
    width       INTEGER,
    height      INTEGER,
    duration_s  REAL,
    local_path  TEXT,                       -- 若已落盘
    signed_url  TEXT,
    url_expires_at REAL,
    created_at  REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

-- 账号级/模型级成本观测：用于"多模型性价比"决策
CREATE TABLE IF NOT EXISTS cost_matrix (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id  TEXT,
    model       TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    resolution  TEXT,
    aspect_ratio TEXT,
    credits     REAL,
    seconds     REAL,
    ok          INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT,
    endpoint   TEXT,
    method     TEXT,
    status     INTEGER,
    code       TEXT,
    ok         INTEGER,
    ms         REAL,
    note       TEXT,
    ts         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_task    ON media(task_id);
CREATE INDEX IF NOT EXISTS idx_cost_model    ON cost_matrix(model, media_type);
CREATE INDEX IF NOT EXISTS idx_reqlog_ts      ON request_log(ts DESC);

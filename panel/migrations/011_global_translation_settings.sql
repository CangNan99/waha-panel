CREATE TABLE IF NOT EXISTS global_translation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    provider TEXT NOT NULL CHECK (provider IN ('aliyun_professional', 'ai_model')),
    scene TEXT NOT NULL DEFAULT 'communication' CHECK (scene IN ('communication', 'social')),
    updated_at INTEGER NOT NULL
);

INSERT OR IGNORE INTO global_translation_settings (id, provider, scene, updated_at)
VALUES (1, 'aliyun_professional', 'communication', strftime('%s', 'now'));

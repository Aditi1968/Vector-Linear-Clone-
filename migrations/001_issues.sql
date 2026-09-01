CREATE TABLE issues (
    id UUID PRIMARY KEY DEFAULT uuidv7(),

    title TEXT NOT NULL,
    description TEXT,

    priority SMALLINT NOT NULL DEFAULT 0,

    completed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT issues_priority_range
        CHECK (priority BETWEEN 0 AND 4)
);

CREATE INDEX issues_created_at_id_idx
    ON issues (created_at DESC, id DESC);
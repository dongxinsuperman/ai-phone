-- functionMapContext / function_map_context
-- Run-level read-only execution reference for function maps, test data,
-- business background, and exception handling notes.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'submissions'
          AND column_name = 'function_map_context'
    ) THEN
        ALTER TABLE public.submissions ADD COLUMN function_map_context TEXT NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'runs'
          AND column_name = 'function_map_context'
    ) THEN
        ALTER TABLE public.runs ADD COLUMN function_map_context TEXT NULL;
    END IF;
END $$;


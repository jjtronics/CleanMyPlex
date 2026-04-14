---
name: csv-and-background-tasks
description: 'Work on CleanMyPlex CSV generation, duplicate detection outputs, task threads, task_status polling, and CSV view or processing routes. Use when changing clean, duplicates, process_csv, delete_csv, view_csv, or threaded task functions.'
argument-hint: 'Describe the CSV or background-task workflow to change in CleanMyPlex'
user-invocable: true
---

# CSV And Background Tasks

## When To Use
- Changing CSV generation rules
- Updating duplicate comparison output
- Modifying background thread behavior
- Debugging task_status progress issues
- Updating CSV view, download, or deletion flows

## Project Facts
- CSV filenames are defined as module-level constants in cleanmyplex.py.
- Long operations use thread helpers such as generate_csv_thread(), delete_items_from_csv_thread(), and compare_libraries_thread().
- Task state is stored in the global tasks dictionary protected by tasks_lock.
- CSV UI flows are split across clean, duplicates, view_csv, process_csv, and delete_csv routes plus their templates.

## Procedure
1. Inspect the route entry point and the thread helper together.
2. Reuse the existing CSV constants instead of adding ad hoc filenames.
3. Keep task creation, status updates, and front-end polling behavior aligned.
4. If a CSV schema changes, verify the related template or processing route still expects the same columns.
5. Validate changed Python and template files after edits.

## Guardrails
- Avoid introducing a new async framework unless requested.
- Be careful with shared global task state and shared CSV files.
- Prefer minimal changes because the current app is a single-file Flask application.
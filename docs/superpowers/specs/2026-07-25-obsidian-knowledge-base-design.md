# Agent Memory Obsidian Knowledge Base Design

## Goal

Set up `/Users/heisnberg/Documents/nyxsuite v5/Agent Memory` as an Obsidian-backed project knowledge base for shared agent memory, project decisions, workflows, commands, issues, architecture notes, troubleshooting, and release notes.

## Design

The vault root is the private `Agent Memory/` folder. Local Obsidian metadata lives in `Agent Memory/.obsidian/` and is ignored by git. Private memory lives in `Agent Memory/` and is also ignored by git.

`AGENTS.md` is the tracked pointer that tells future authorized agents to read `Agent Memory/00 Dashboard.md` and `Agent Memory/Agent Memory.md` before making project changes. The dashboard links to all major knowledge-base notes.

Obsidian core settings should open the dashboard, store attachments in `Attachments`, put new notes in `Inbox`, and configure templates from `Templates`, all relative to the Agent Memory vault.

## Scope

Included: local vault settings, knowledge-base Markdown scaffolding, templates, and project instructions.

Excluded: syncing, publishing, third-party Obsidian community plugins, credentials, secrets, and automated commits.

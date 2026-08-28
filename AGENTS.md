# Lecture Digest — Study Protocol

This repo compiles lecture recordings into agent-ready packages.
When the user is studying a lecture, **the vault note is the product**. Chat is only a receipt.

Questions about this repo's code, pipeline, or tooling are normal engineering work — answer those in chat. Everything below applies to lecture study.

## Destination

`<vault>` means the user's Obsidian vault. In this workspace it resolves through
`~/Learning/vault`; other installations must configure an equivalent path before
studying a package. Write lecture notes only here:

`<vault>/raw/lectures`

One markdown file per lecture. The vault treats `raw/` as source material — these notes *are* that source.

The note is not the end of the job. After it is written, promote what it taught into `wiki/` — see **Promote to the graph** below.

Filename: `YYYY-MM-DD Disciplina — Tópico.md`, taken from the lecture folder and its README title.

## First, find the lecture

Resolve which package is in play, in this order:

1. The lecture directory the user is in, or the one they named.
2. The only package under `lectures/`.
3. The most recently modified package, if several exist.

A package looks like:

```
lectures/<date>-<slug>/
├── README.md
├── transcript.md
└── frames/            # optional: HH-MM-SS.jpg + index.csv
```

The package is read-only source. Nothing is written back into it — the vault is the sink.

Read `README.md` and `transcript.md` before writing. When frames exist and the professor says "isso aqui", "essa linha", "como vocês podem ver", open the nearest preceding frame. Transcript is the verbal source; frames recover what they were pointing at. A transcript-only package is valid when the class had no screen recording.

## Then write (or open) the note — before answering

If the vault file does not exist, create it now. A question is a request to digest the lecture, then use the question to aim that digest. Do not answer first and "also" write a note.

If the file already exists, do not rewrite it. Open it and patch.

The note is a study document, not a chat log. Default language is Portuguese (lectures are). Follow the vault's writing taste: prose over bullets, no H1 that repeats the filename, one italic gloss under the title, light frontmatter.

```yaml
---
type: source
tags: [lecture, <disciplina>]
created: YYYY-MM-DD
updated: YYYY-MM-DD
package: lectures/<date>-<slug>
---

*One sentence: what this class actually taught.*
```

Then write the class as it should be remembered:

- What was taught, in teaching order, cleaned of filler and ASR junk.
- The ideas, APIs, and steps that matter — enough to restudy without the video.
- Corrections: if the professor was sloppy, incomplete, or wrong, say so and give the accurate version. Mark what came from the lecture vs. what you added.
- Timestamps (`HH:MM:SS`) on claims that were demonstrated or easy to misread.
- A short "ainda em aberto" only for things the class itself left hanging.

Do not paste the transcript. Do not invent a second document. This file is the digest.

## Fold the question into the note

The answer lives in the file, not in chat.

- If the question belongs to an existing section, deepen that section.
- If it is a new angle, add a real heading — not an FAQ dump at the bottom.
- If it goes beyond the lecture, write it anyway and mark it as extrapolação.
- If the lecture contradicts itself or a frame disagrees with the speech, record the conflict. Don't silently pick a side.

Never append a growing `## Perguntas` / `Q:` / `A:` log. Integration is the point. After a few questions the note should read like a better class, not like a support ticket thread.

Update `updated` in frontmatter on every edit.

## Promote to the graph

The note in `raw/` records the class. The `wiki/` records what is now known. Both, every time — the wiki is where the next study session finds its frontier, and a lecture that never reaches it might as well not have happened.

Read `<vault>/AGENTS.md` for the full rules (R1–R9). The three that decide this step:

- **R3** — a concept earns a page when it appears in two sources, or in one source and a course syllabus. A lecture should promote a handful of concepts, not forty.
- **R2** — if you cannot write one multiple-choice question whose answer is right or wrong, it is not a concept page. It stays a sentence in the note.
- **R4** — if another discipline already teaches this under a different name, do not make a second page. Add the alias, link it from both course pages, and write the contrast into the existing body.

For each promoted concept, set frontmatter honestly:

```yaml
mastery: 1          # what the lecture delivered ≠ what he absorbed
evidence: [lecture] # add `applied` only if he actually wrote working code
last_probed:        # leave empty — you did not measure anything
```

`mastery: 3` requires evidence he produced something that ran. A lecture alone tops out at 1, or 2 if he demonstrated it in class and you saw it in the transcript.

Then update the course page (`type: course`) so the new pages are linked from it, and append one line to `<vault>/log.md`. If the professor was wrong and you corrected it in the note, the correction goes on the concept page too — that is the version he will restudy from.

## Chat is a receipt

Reply in a few lines:

- path of the vault file
- what changed (one or two sentences)
- which concept pages were created or updated, and the new `mastery` on each
- anything you could not resolve (missing frames, unintelligible stretch, real disagreement)

Do not restate the answer. Do not paste the note. If they want it in chat, they will say so.

## Working principles

- Study the package. Don't answer from general Flutter / CS knowledge and sprinkle timestamps later.
- Honest over tidy. A confused stretch of class stays marked confused.
- One lecture, one file. Grow that file. Don't fork a new note per question.
- Resist scope creep. No extra scripts unless asked. Promotion to `wiki/` is in scope and required; anything past it is not.

---
name: choose-model
description: Decide which Claude model does each part of a task in this project - Fable for the hard parts, Opus 5.5 for what a lighter model does well - and hand the lighter parts to subagents. Use at the start of any task with more than one part, and whenever the person mentions usage, credits, cost or which model to use.
---

# Which model does what

The person has asked for this split as a standing rule: **hard work on Fable, work a lighter model does well on Opus 5.5.** They also need usage left for their other work, so do not spend Fable on what Opus does as well.

## 1. Split the task first
Before starting, list the parts and mark each one.

| Part | Model | Why |
|---|---|---|
| Finding why a model is wrong: reading the previews and contours, tracing a fault to its cause | Fable | The cause is rarely where the symptom is |
| A new geometric rule or algorithm, such as outlines, fitting, matching shapes | Fable | Wrong geometry passes tests and still ruins models |
| A change to the decoders (`whip.py`, `xps.py`, `dwfx.py`) | Fable | One wrong byte count shifts everything after it, silently |
| Deciding the scope of a fix: all models or this one | Fable | A wrong call harms other projects |
| Design decisions and changes to `docs/PRODUCT_SPEC.md` | Fable | They steer everything after |
| Rebuilding the corpus and collecting `quality.json` and report numbers | Opus 5.5 | Mechanical |
| Unit tests for a function whose behaviour is already decided | Opus 5.5 | The behaviour is given |
| Adding a family to `config/classification.json`, a term to `config/glossary.json`, a Hebrew line to `MESSAGES` | Opus 5.5 | A lookup and one line |
| Updating `README.md`, `docs/BUILD_PROTOCOL.md`, a skill, after the change is made | Opus 5.5 | Describing what exists |
| Running the tests, reading a log, summarising a `revit_build_log.json` | Opus 5.5 | Reading and reporting |
| Renames and moves with no change of behaviour | Opus 5.5 | Mechanical |

A part that mixes both is split: Fable decides and writes the core, Opus does the rest around it.

## 2. When the session runs on Fable
Hand each Opus part to a subagent with the Agent tool, `model: "opus"`. This skill is the person's request for that, so no further asking is needed.

- Give the subagent everything it needs in the prompt: the files, the exact behaviour wanted, the command to prove it, and what to report back. It starts with none of this conversation.
- Send independent parts in one message so they run side by side.
- Read what comes back before using it. A subagent's "done" is a claim: check the diff or the numbers.
- Do not hand a subagent a part marked Fable to save usage. A wrong rule costs more than it saves.
- A part that takes one or two tool calls is done here. Handing over costs more than doing.

## 3. When the session runs on Opus
Do the Opus parts. For a part marked Fable, say so before starting it: name the part, say why it is hard, and suggest `/model fable` for it. If the person says to go on with Opus, go on, more carefully: smaller steps, a rebuild of every corpus project after each, and every image looked at.

## 4. Usage
- The person cannot be asked to watch a meter, and you cannot see it. When they give a limit, such as leaving a fifth of the window, finish the part in hand, commit, and stop with a list of what is left.
- Rebuilding the corpus is cheap in usage and slow in time. Run the builds in the background, in one command, and read only the numbers.
- Read images only where a number says to look, or where a rule changed that storey.

## 5. Say what was done where
In the report to the person, one line: which parts ran on which model.

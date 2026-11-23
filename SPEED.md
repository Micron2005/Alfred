# Making Alfred fast, all local

The goal: the quickest possible fully-local Alfred on an 8GB card. Speed here
is not one trick — it is refusing to make him wait for things he does not
need, and respecting the VRAM ceiling.

## What was done

1. **keep_alive — the biggest single win.** Ollama unloads a model after 5
   minutes idle by default, so the first message after any pause paid a full
   model reload (10-30s on this card). Alfred now sets keep_alive (30m by
   default; "-1" pins it forever) so his voice never goes cold between
   messages. Configured in desktop.toml.

2. **Simple greetings and acknowledgments skip the planner.** "Good evening"
   or "thanks" need only a reply. Other messages go to the planner when
   capabilities are available, even if no work verb is present. It gets a
   short excerpt of the current conversation so "do the second one" can
   refer to a previous turn.

3. **Learning moved to the background.** Noticing durable facts about you no
   longer blocks the reply. You get the answer; Alfred files what he learned
   a moment later, off the critical path.

4. **Context and output caps.** num_ctx=4096 and num_predict=1024 in
   desktop.toml keep the context small (faster on 8GB, ample for
   conversation) and cap runaway replies. Raise them for long technical work.

## Tuning knobs (configs/desktop.toml)

    keep_alive  = "30m"   # "-1" never unloads; "0" unloads immediately
    num_ctx     = 4096    # bigger = more memory of the turn, slower
    num_predict = 1024    # max reply tokens

## The bigger lever, when hardware allows

The model itself is the floor on speed. qwen2.5:7b is a good fit for 8GB. If
you later want faster-and-lighter, qwen2.5:3b nearly doubles throughput at
some quality cost; if you get a bigger card, a larger model or a longer
num_ctx becomes affordable. None of this changes the architecture — it is one
line in desktop.toml. And when the GPU path works (ROCm, or llama.cpp Vulkan),
inference moves off the CPU entirely, which is the largest win of all.

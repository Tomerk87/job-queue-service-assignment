# AI Tool Usage

## Tools I Used

**ChatGPT** — Used during the planning and design stages to analyze requirements, evaluate architectural alternatives, explore edge cases, and define acceptance criteria. After architecture decision, I structured the implementation into six phases, each with clearly defined tasks, tests, and completion criteria.

**Claude Code** — Used for the implementation through a vibe-coding workflow, including writing code, developing and executing tests, verifying edge cases, and documenting results. Development was performed incrementally, with each phase I reviewed and approved before proceeding to the next.

## What Helped Most

**1. Architectural analysis and trade-offs**

Drawing on my previous experience with Redis, I explored different approaches to implementing the job queue, whether using classic queue solutions such as Kafka, RabbitMQ etc. Discussions with ChatGPT helped evaluate PostgreSQL, Redis, and dedicated queue technologies in terms of implementation time, complexity, reliability, and failure recovery.

I ultimately chose PostgreSQL as the source of truth for job state and claiming, with Redis used for worker notifications. This provided a relatively simple architecture while maintaining clear consistency guarantees.

**2. Incremental development and concurrency testing**

AI-assisted development helped turn the technical specification into manageable implementation phases, each with defined tests and acceptance criteria.

I specifically required concurrency tests with multiple workers running simultaneously to verify that jobs could not be claimed by more than one worker at the same time.

I also emphasized testing real failure scenarios, including worker crashes, lease expiration, stale workers, and Redis unavailability, rather than relying only on mocked behavior or successful execution paths.

**3. Applying Backend Engineering Experience Across Languages**

My professional backend development experience is primarily in Java, with only limited prior exposure to Python through small scripts.

One of the most valuable aspects of AI-assisted development was the ability to apply my existing engineering knowledge to a language outside my usual technology stack.

Using Claude Code, I could focus on system architecture, concurrency, failure handling, and technical trade-offs, while AI assisted with Python-specific syntax, libraries, and implementation patterns.

This allowed me to build on my existing backend engineering experience rather than having the programming language itself become the main obstacle. It also required me to review and understand the generated code, particularly around database transactions, worker coordination, and error handling.

## What I Had to Fix

**1. Handling scheduled jobs with timestamps in the past**

During the design discussion, an initial suggestion was to immediately execute jobs whose scheduled execution time was already in the past.

I challenged this behavior by raising the case of a job submitted with a timestamp from several months earlier.

After discussing alternative approaches, I chose to introduce a limited tolerance window of 30 seconds to accommodate minor clock differences and network delays, while rejecting significantly outdated scheduled timestamps.

**2. Recovering from a stuck worker**

I raised the scenario of a worker becoming permanently stuck while processing a job, rather than simply failing or crashing.

We examined why cooperative timeouts and lease expiration alone might be insufficient, particularly when only one worker is running.

I chose an approach combining execution deadlines, heartbeat cutoff, and worker process termination for persistent hangs, allowing Docker to restart the process and the recovery mechanism to reclaim the job.

These were design-level refinements made through critical review of AI suggestions, rather than corrections of confirmed implementation bugs.

## What AI Struggled With

**Ambiguous requirements and edge-case decisions**

Several requirements allowed multiple valid interpretations, particularly around scheduling, retries, worker ownership, timeouts, and failure handling.

Before each implementation phase, I reviewed the proposed approach with Claude Code, discussed relevant edge cases, and made explicit decisions about the expected behavior.

This helped reduce ambiguity and prevent assumptions from becoming implementation decisions without review.

**Distributed systems guarantees and trade-offs**

An important area requiring careful consideration was distinguishing between preventing duplicate job claims, protecting database state from stale workers, and guaranteeing that external side effects execute only once.

The implementation provides at-least-once processing semantics with safeguards around job ownership and database updates, but it does not guarantee exactly-once execution of external side effects.

Recognizing and documenting these limitations was an important part of reviewing the AI-assisted design.
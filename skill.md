# Prompt Optimizer

Analyze a draft prompt, evaluate it, match it to ECC ecosystem components, and output a complete optimized prompt for the user to copy, paste, and run.

## When to Use

- User says "optimize this prompt", "improve my prompt", "rewrite this prompt"
- User says "help me write a better prompt to..."
- User says "what is the best way to ask Claude Code to..."
- User says "optimize prompt", "improve prompt", "how to write a prompt", "help me optimize this instruction"
- User pastes a draft prompt and asks for feedback or improvements
- User says "I don't know how to write a prompt for this"
- User says "how should I use ECC to..."
- User explicitly invokes `/prompt-optimize`

### Do Not Use When

- User wants to execute the task directly (execute it directly instead)
- User says "optimize code", "optimize performance", "optimize this code", "optimize performance" — these are refactoring tasks, not prompt optimization
- User asks about ECC configuration (use `configure-ecc` instead)
- User wants an inventory of skills (use `skill-stocktake` instead)
- User says "just do it"

## How It Works

**Advisory only — do not execute the user's task.**

Do not write code, create files, run commands, or take any implementation actions. Your **sole** output is the analysis plus an optimized prompt.

If the user says "just do it" or "don't optimize, execute directly", do not switch into implementation mode within this skill. Inform the user that this skill only generates optimized prompts, and instruct them to make a standard task request if they want to execute the task.

Run this 6-phase process sequentially. Present the results using the output format below.

### Analysis Process

### Phase 0: Project Detection

Detect current project context before analyzing the prompt:

1. Check whether `CLAUDE.md` exists in the working directory — read it to understand project conventions.
2. Detect the tech stack from project files:
   - `package.json` → Node.js / TypeScript / React / Next.js
   - `go.mod` → Go
   - `pyproject.toml` / `requirements.txt` → Python
   - `Cargo.toml` → Rust
   - `build.gradle` / `pom.xml` → Java / Kotlin (then check build files for `quarkus` → Quarkus, or `spring-boot` → Spring Boot)
   - `Package.swift` → Swift
   - `Gemfile` → Ruby
   - `composer.json` → PHP
   - `*.csproj` / `*.sln` → .NET
   - `Makefile` / `CMakeLists.txt` → C / C++
   - `cpanfile` / `Makefile.PL` → Perl
3. Record the detected tech stack for use in Phase 3 and Phase 4.

If no project files are found (e.g., the prompt is abstract or intended for a new project), skip detection and flag "Tech stack unknown" in Phase 4.

### Phase 1: Intent Detection

Classify the user's task into one or more categories:

| Category       | Signal Words                          | Example                  |
| -------------- | ------------------------------------- | ------------------------ |
| New Feature    | build, create, add, implement         | "Build a login page"     |
| Bug Fix        | fix, broken, not working, error       | "Fix the auth flow"      |
| Refactor       | refactor, clean up, restructure       | "Refactor the API layer" |
| Research       | how to, what is, explore, investigate | "How to add SSO"         |
| Testing        | test, coverage, verify                | "Add tests for the cart" |
| Review         | review, audit, check                  | "Review my PR"           |
| Documentation  | document, update docs                 | "Update the API docs"    |
| Infrastructure | deploy, CI, docker, database          | "Set up CI/CD pipeline"  |
| Design         | design, architecture, plan            | "Design the data model"  |

### Phase 2: Scope Assessment

If Phase 0 detects a project, use codebase size as a signal. Otherwise, estimate based purely on the prompt description and flag the estimate as uncertain.

| Scope  | Heuristic                                      | Orchestration                                |
| ------ | ---------------------------------------------- | -------------------------------------------- |
| Tiny   | Single file, < 50 lines                        | Direct execution                             |
| Low    | Single component or module                     | Single command or skill                      |
| Medium | Multiple components, same domain               | Command chain + /verify                      |
| High   | Cross-domain, 5+ files                         | Use /plan first, then execute in phases      |
| Epic   | Multi-session, multi-PR, architectural changes | Multi-session plan using the blueprint skill |

### Phase 3: ECC Component Matching

Map intent + scope + tech stack (from Phase 0) to specific ECC components.

#### By Intent Type

| Intent            | Commands                               | Skills                                                    | Agents                            |
| ----------------- | -------------------------------------- | --------------------------------------------------------- | --------------------------------- |
| New Feature       | /plan, /tdd, /code-review, /verify     | tdd-workflow, verification-loop                           | planner, tdd-guide, code-reviewer |
| Bug Fix           | /tdd, /build-fix, /verify              | tdd-workflow                                              | tdd-guide, build-error-resolver   |
| Refactor          | /refactor-clean, /code-review, /verify | verification-loop                                         | refactor-cleaner, code-reviewer   |
| Research          | /plan                                  | search-first, iterative-retrieval                         | —                                 |
| Testing           | /tdd, /e2e, /test-coverage             | tdd-workflow, e2e-testing                                 | tdd-guide, e2e-runner             |
| Review            | /code-review                           | security-review                                           | code-reviewer, security-reviewer  |
| Documentation     | /update-docs, /update-codemaps         | —                                                         | doc-updater                       |
| Infrastructure    | /plan, /verify                         | docker-patterns, deployment-patterns, database-migrations | architect                         |
| Design (Med-High) | /plan                                  | —                                                         | planner, architect                |
| Design (Epic)     | —                                      | blueprint (invoked as a skill)                            | planner, architect                |

#### By Tech Stack

| Tech Stack         | Skills to Add                                                                                                          | Agents                         |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| Python / Django    | django-patterns, django-tdd, django-security, django-verification, python-patterns, python-testing                     | python-reviewer                |
| Go                 | golang-patterns, golang-testing                                                                                        | go-reviewer, go-build-resolver |
| Spring Boot / Java | springboot-patterns, springboot-tdd, springboot-security, springboot-verification, java-coding-standards, jpa-patterns | java-reviewer                  |
| Quarkus / Java     | quarkus-patterns, quarkus-tdd, quarkus-security, quarkus-verification, java-coding-standards, jpa-patterns             | java-reviewer                  |
| Kotlin / Android   | kotlin-coroutines-flows, compose-multiplatform-patterns, android-clean-architecture                                    | kotlin-reviewer                |
| TypeScript / React | frontend-patterns, backend-patterns, coding-standards                                                                  | code-reviewer                  |
| Swift / iOS        | swiftui-patterns, swift-concurrency-6-2, swift-actor-persistence, swift-protocol-di-testing                            | code-reviewer                  |
| PostgreSQL         | postgres-patterns, database-migrations                                                                                 | database-reviewer              |
| Perl               | perl-patterns, perl-testing, perl-security                                                                             | code-reviewer                  |
| C++                | cpp-coding-standards, cpp-testing                                                                                      | code-reviewer                  |
| Other / Unlisted   | coding-standards (generic)                                                                                             | code-reviewer                  |

### Phase 4: Missing Context Detection

Scan the prompt for missing critical details. Check each item and flag whether it was auto-detected in Phase 0 or must be provided by the user:

- [ ] **Tech Stack** — Detected in Phase 0, or must the user specify?
- [ ] **Target Scope** — Are specific files, directories, or modules mentioned?
- [ ] **Acceptance Criteria** — How do we know the task is complete?
- [ ] **Error Handling** — Are edge cases and failure modes accounted for?
- [ ] **Security Requirements** — Authentication, input validation, secrets?
- [ ] **Testing Expectations** — Unit, integration, E2E?
- [ ] **Performance Constraints** — Load, latency, resource limits?
- [ ] **UI/UX Requirements** — Design specs, responsiveness, accessibility? (if frontend)
- [ ] **Database Changes** — Schemas, migrations, indexes? (if data tier)
- [ ] **Existing Patterns** — Reference files or conventions to follow?
- [ ] **Scope Boundaries** — What should **not** be done?

**If 3 or more critical items are missing**, ask the user up to 3 clarifying questions before generating the optimized prompt. Then incorporate their answers into the optimized prompt.

### Phase 5: Workflow and Model Recommendations

Determine where this prompt fits in the development lifecycle:

```
Research → Plan → Implement (TDD) → Review → Verify → Commit
```

For medium-tier tasks and above, always start with `/plan`. For epic tasks, use the blueprint skill.

**Model Recommendations** (included in the output):

| Scope    | Recommended Model                         | Rationale                                        |
| -------- | ----------------------------------------- | ------------------------------------------------ |
| Tiny–Low | Sonnet 5                                  | Fast, cost-effective for simple tasks            |
| Medium   | Sonnet 5                                  | Best coding model for standard work              |
| High     | Sonnet 5 (Main) + Opus 5 (Planning)       | Opus for architecture, Sonnet for implementation |
| Epic     | Opus 5 (Blueprint) + Sonnet 5 (Execution) | Deep reasoning for multi-session planning        |

**Multi-Prompt Splitting** (for High / Epic scope):

For tasks exceeding a single session, split into sequential prompts:

- Prompt 1: Research + Plan (use search-first skill, then `/plan`)
- Prompts 2–N: Implement one phase per prompt (each phase ends with `/verify`)
- Final Prompt: Integration testing + `/code-review` across all phases
- Use `/save-session` and `/resume-session` to preserve context across sessions

---

## Output Format

Present your analysis following this exact structure. Respond in the same language as the user's input.

### Part 1: Prompt Diagnosis

**Strengths:** What the original prompt does well.

**Issues:**

| Issue   | Impact        | Suggested Fix |
| ------- | ------------- | ------------- |
| (Issue) | (Consequence) | (How to fix)  |

**Clarifications Needed:** Numbered list of questions the user should answer. If an answer was auto-detected in Phase 0, state that answer instead of asking.

### Part 2: Recommended ECC Components

| Type    | Component     | Purpose                          |
| ------- | ------------- | -------------------------------- |
| Command | /plan         | Plan architecture before coding  |
| Skill   | tdd-workflow  | Guide TDD methodology            |
| Agent   | code-reviewer | Post-implementation review       |
| Model   | Sonnet 5      | Recommended model for this scope |

### Part 3: Optimized Prompt — Full Version

Present the complete optimized prompt inside a single fenced code block. The prompt must be self-contained and copy-paste ready. Include:

- Clear task description and context
- Tech stack (detected or specified)
- `/command` calls at the correct workflow stages
- Acceptance criteria
- Verification steps
- Scope boundaries (what **not** to do)

For items referencing blueprints, write: "Use the blueprint skill to..." (rather than `/blueprint`, since blueprint is a skill, not a command).

### Part 4: Optimized Prompt — Quick Version

A compact version for experienced ECC users. Varies by intent type:

| Intent        | Quick Mode                                                                  |
| ------------- | --------------------------------------------------------------------------- |
| New Feature   | `/plan [feature]. /tdd to implement. /code-review. /verify.`                |
| Bug Fix       | `/tdd — write failing test for [bug]. Fix to green. /verify.`               |
| Refactor      | `/refactor-clean [scope]. /code-review. /verify.`                           |
| Research      | `Use search-first skill for [topic]. /plan based on findings.`              |
| Testing       | `/tdd [module]. /e2e for critical flows. /test-coverage.`                   |
| Review        | `/code-review. Then use security-reviewer agent.`                           |
| Documentation | `/update-docs. /update-codemaps.`                                           |
| Epic          | `Use blueprint skill for "[objective]". Execute phases with /verify gates.` |

### Part 5: Improvement Rationale

| Improvement      | Rationale        |
| ---------------- | ---------------- |
| (What was added) | (Why it matters) |

### Footer

> Not quite what you need? Let me know what to adjust, or submit a regular task request if you'd like to execute the task rather than optimize the prompt.

---

## Examples

### Trigger Examples

- "Optimize this prompt for ECC"
- "Rewrite this prompt so Claude Code uses the right commands"
- "Help me optimize this instruction"
- "How should I prompt ECC for this task?"

### Example 1: Vague Prompt (Project Detected)

**User Input:**

```
Help me build a user login page
```

**Phase 0 Detected:** `package.json`, using Next.js 15, TypeScript, Tailwind CSS

**Optimized Prompt (Full):**

```
Implement a user login page using the project's existing tech stack (Next.js 15 + TypeScript + Tailwind CSS).

Technical Requirements:
- Follow existing project component structure and routing conventions
- Form validation: Use the project's existing validation library (check for Zod/Yup/etc.)
- Authentication: Follow the existing authentication scheme (default to JWT if none present)
- Include: Email/password form, client-side validation, error handling, loading states, responsive layout

Workflow:
1. /plan the component structure and auth flow, referencing patterns in existing pages
2. /tdd test-first: write unit tests for the login form and integration tests for auth flow
3. Implement the login page and auth logic
4. /code-review the implementation
5. /verify that all tests pass and the page renders correctly

Security Requirements:
- Passwords must never be transmitted in plain text
- Rate limiting protection against brute force attacks
- XSS prevention
- CSRF token validation

Acceptance Criteria:
- All tests pass with 80%+ coverage
- Page renders correctly across mobile and desktop breakpoints
- Successful login redirects to dashboard; failure displays clear error messaging

Out of Scope:
- Do not build a registration page
- Do not implement password recovery/reset flows
- Do not modify existing routing architecture
```

### Example 2: Medium Prompt

**User Input:**

```
Add a REST API endpoint for updating user profiles, including validation.
```

**Phase 0 Detected:** `go.mod`, using Go 1.22, Chi router

**Optimized Prompt (Full):**

```
Add a REST API endpoint for user profile updates (PATCH /api/users/:id).

Tech Stack: Go 1.22 + Chi router (detected from project)

Requirements:
- PATCH /api/users/:id — partial update of user profile
- Input validation on fields: name, email, avatar_url, bio
- Authentication: requires valid bearer token; users can only update their own profile
- Return 200 with updated user payload on success
- Return 400 with validation errors on invalid input
- Return 401/403 on authentication/authorization failure
- Follow existing API patterns across the codebase

Workflow:
1. /plan endpoint structure, middleware chain, and validation logic
2. /tdd — write table-driven tests for success, validation failure, auth failure, not found
3. Implement following existing handler patterns
4. /go-review
5. /verify — run full test suite and confirm no regressions

Out of Scope:
- Do not modify existing endpoints
- Do not alter the database schema (use existing user table)
- Do not add new external dependencies without checking existing ones first (use search-first skill)
```

### Example 3: Epic Project

**User Input:**

```
Migrate our monolith to microservices
```

**Optimized Prompt (Full):**

```
Use the blueprint skill to plan: "Migrate monolith architecture to microservices"

Before execution, address the following in the blueprint:
1. What domain boundaries exist in the current monolith?
2. Which service should be extracted first (lowest coupling)?
3. Communication patterns: REST APIs, gRPC, or event-driven (Kafka/RabbitMQ)?
4. Database strategy: Shared database initially, or database-per-service from day one?
5. Deployment target: Kubernetes, Docker Compose, or serverless?

The blueprint should generate phases structured as follows:
- Phase 1: Identify service boundaries and create domain maps
- Phase 2: Set up foundational infrastructure (API gateway, service mesh, per-service CI/CD)
- Phase 3: Extract first service using the Strangler Fig pattern
- Phase 4: Validate via integration tests, then proceed to next service extraction
- Phase N: Decommission monolith components

Each Phase = 1 PR, gated by /verify checkpoints.
Use /save-session between phases. Resume with /resume-session.
Use git worktrees for parallel service extraction when dependency graphs allow.

Recommendation: Plan blueprint using Opus 5, execute phases with Sonnet 5.
```

---

## Related Components

| Component                 | When to Reference                                                   |
| ------------------------- | ------------------------------------------------------------------- |
| `configure-ecc`           | User has not configured ECC yet                                     |
| `skill-stocktake`         | Audit installed components (use this instead of hardcoded catalogs) |
| `search-first`            | Research phase in optimized prompts                                 |
| `blueprint`               | Epic-scoped prompts (invoked as a skill, not a command)             |
| `strategic-compact`       | Long-session context management                                     |
| `cost-aware-llm-pipeline` | Token optimization recommendations                                  |

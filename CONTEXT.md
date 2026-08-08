# Context: Gitea to GitHub Automatic Push Mirror Management Using Woodpecker CI

## 1. Project Goal

Build an automated GitHub synchronization workflow based on:

- Gitea as the primary local Git hosting service
- GitHub as an optional public backup / distribution mirror
- Woodpecker CI as the automation execution framework

The goal is to avoid manually configuring Gitea Push Mirror for every repository.

Current manual workflow:

1. Create repository in Gitea
2. Create corresponding repository manually on GitHub
3. Enter Gitea repository settings
4. Configure Push Mirror URL
5. Input GitHub Personal Access Token
6. Repeat for every repository

Target workflow:

1. Create repository in Gitea
2. Add a repository-level CI configuration indicating GitHub synchronization
3. Push code to Gitea
4. Woodpecker CI automatically:
   - checks GitHub repository existence
   - creates GitHub repository if necessary
   - configures Gitea Push Mirror if necessary
5. Future pushes are automatically mirrored by Gitea Push Mirror

---

## 2. Current Environment

### Gitea

Version:

```text
Gitea 1.27.1
built with go1.26.5-X:jsonv2
```

Current installation:

```text
snap package
```

Executable:

```text
/snap/bin/gitea
```

Possible future migration:

Official binary installation:

<https://docs.gitea.com/zh-cn/installation/install-from-binary>

with dedicated system user:

```text
gitea
```

The implementation should avoid depending on snap-specific paths.

### Operating System

Server:

```text
Ubuntu 20.04 LTS
```

### CI

Woodpecker CI will be deployed on the same Ubuntu 20.04 server as Gitea.

Architecture:

```text
Gitea
  |
  | webhook
  v
Woodpecker Server
  |
  v
Woodpecker Agent/Runner
  |
  v
Synchronization Action
```

---

## 3. Final Architecture Decision

### Overall Architecture

```text
Developer PC
  |
  | git push
  v
Gitea
  |
  | webhook
  v
Woodpecker CI
  |
  +-------------------+
  |                   |
  v                   v
GitHub API         Gitea API
  |                   |
  |                   |
Create repo       Configure Push Mirror
                      |
                      v
               GitHub Repository
```

---

## 4. Repository Synchronization Trigger

### Default Behavior

All repositories:

```text
NO synchronization
```

Only repositories explicitly configured will sync.

### Enable Synchronization

A repository must contain:

```text
.woodpecker.yml
```

This file means:

> This repository participates in automation and GitHub synchronization.

Example:

```text
project/
|-- README.md
|-- src/
`-- .woodpecker.yml
```

Repositories without:

```text
.woodpecker.yml
```

will not trigger synchronization.

---

## 5. Synchronization Trigger Timing

Selected mode:

### A. Git push immediately triggers synchronization

Workflow:

```text
git push Gitea
    |
    v
Woodpecker trigger
    |
    v
Synchronization process
    |
    v
GitHub mirror ready
```

---

## 6. Synchronization Logic

Every push should verify mirror state.

Selected strategy:

### Check and ensure mirror exists every push

For every triggered synchronization:

1. Receive repository information from Woodpecker.
2. Query GitHub API.
3. Check whether GitHub repository exists.
4. If the GitHub repository does not exist, create it automatically.
5. Query Gitea API.
6. Check whether Push Mirror exists.
7. If the Push Mirror is missing, create it automatically.
8. Exit successfully.

The system should be idempotent:

Running synchronization repeatedly must not create duplicates or corrupt configuration.

---

## 7. GitHub Repository Rules

### Target Account

Fixed personal GitHub account.

Example:

```text
Gitea:
user/project

GitHub:
github_username/project
```

No multi-account or organization support is required initially.

### Repository Name

Default:

Same as Gitea repository name.

Example:

```text
Gitea:
px4-tools

GitHub:
github_username/px4-tools
```

### Visibility

Controlled by repository configuration.

Default:

Public.

Example configuration:

```yaml
github:
  enabled: true
  private: false
```

Private repository:

```yaml
github:
  enabled: true
  private: true
```

Default behavior:

```text
private=false
```

---

## 8. Credential Management

Credentials must NOT be stored in repository files.

Required secrets:

### GitHub

Personal Access Token:

Example:

```text
github_pat_xxxxxxxxx
```

Stored using:

```text
Woodpecker Secrets
```

Not stored:

- in `.woodpecker.yml`
- in source code
- in Git repository

### Gitea

Required:

Gitea API Token

Stored using:

```text
Woodpecker Secret
```

---

## 9. Synchronization Implementation Principle

Do NOT implement:

- long-running custom daemon
- custom Git server
- custom mirror service

Use Woodpecker CI for:

- triggering
- execution lifecycle
- logs
- secrets management

Custom code should only implement:

> GitHub mirror provisioning logic

Expected custom logic:

```text
Input:
repository name
repository owner

Actions:
1. GitHub API: check repository
2. GitHub API: create repository if missing
3. Gitea API: check push mirror
4. Gitea API: create push mirror if missing

Output:
success/failure
```

---

## 10. Expected Repository CI Configuration

Initial target:

Minimal repository configuration.

Example:

```yaml
steps:
  - name: github-sync
    image: sync-tool-image
    commands:
      - sync-github-mirror
```

The synchronization implementation should be centralized.

Do not put:

- GitHub token
- API logic
- mirror URL generation

inside every repository.

---

## 11. Future Extension Possibilities

Possible future features:

### More Configuration

Example:

```text
.github-sync.yml
```

or extension of:

```text
.woodpecker.yml
```

Example:

```yaml
github:
  enabled: true
  private: false
```

Possible future:

- GitHub Release creation
- Docker image publishing
- PX4 firmware build
- ROS build verification
- Automated deployment

The design should keep these extensions possible.

---

## 12. Design Philosophy

Follow these principles:

1. Prefer mature existing systems over custom daemons.
2. Custom code should only be glue logic.
3. CI system manages:
   - execution
   - lifecycle
   - secrets
   - logs
4. Gitea remains the primary repository.
5. GitHub is a controlled external mirror.
6. Synchronization should be declarative: repository configuration determines behavior.

---

## 13. Implementation Tasks

Future implementation should include:

### Phase 1

Deploy:

- Woodpecker Server
- Woodpecker Agent

Integrate with:

- Gitea OAuth
- Gitea webhook

### Phase 2

Create synchronization action.

Functions:

- GitHub repository existence check
- GitHub repository creation
- Gitea Push Mirror creation
- error handling
- logging

### Phase 3

Test cases:

- New Gitea repository
- Existing GitHub repository
- Existing mirror
- Deleted mirror recovery
- Public/private repository creation

---

# End of Context

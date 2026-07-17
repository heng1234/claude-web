# Claude Web Refactor Plan

## Current State Analysis

### Backend (server.py)
- **Size**: 2,556 lines in single file
- **Routes**: 40+ endpoints mixed with business logic
- **Issues**:
  - All code in one file
  - Route handlers contain business logic
  - No separation of concerns
  - Database operations mixed with HTTP handling
  - Difficult to test and maintain

### Frontend (static/index.html)
- **Size**: 16,823 lines in single file
- **Functions**: ~450 functions and classes
- **Sections**: 50+ logical sections
- **Issues**:
  - All HTML, CSS, and JavaScript in one file
  - No modularization
  - Global namespace pollution
  - Impossible to unit test
  - Hard to navigate and maintain

## Refactor Strategy

### Phase 1: Backend Modularization (Priority: HIGH)

#### 1.1 Project Structure
```
claude_web/
├── __init__.py
├── app.py              # Flask app factory
├── config.py           # Configuration management
├── models/             # Data models
│   ├── __init__.py
│   ├── session.py
│   ├── message.py
│   ├── tool_call.py
│   └── user.py
├── routes/             # HTTP endpoints
│   ├── __init__.py
│   ├── chat.py         # Chat operations
│   ├── sessions.py     # Session management
│   ├── auth.py         # Authentication
│   ├── mcp.py          # MCP integration
│   └── files.py        # File operations
├── services/           # Business logic
│   ├── __init__.py
│   ├── chat_service.py
│   ├── session_service.py
│   ├── anthropic_service.py
│   ├── mcp_service.py
│   └── search_service.py
├── utils/              # Utilities
│   ├── __init__.py
│   ├── auth.py
│   ├── file_handler.py
│   └── validators.py
└── static/             # Frontend assets
    └── index.html
```

#### 1.2 Migration Steps

**Step 1: Extract Models** (1-2 hours)
- Create `models/` directory
- Extract database schema definitions
- Add model validation logic
- Target: <200 lines per model file

**Step 2: Extract Services** (2-3 hours)
- Create `services/` directory
- Move business logic from routes
- Add service layer tests
- Target: <400 lines per service file

**Step 3: Extract Routes** (1-2 hours)
- Create `routes/` directory
- Keep routes thin (validation + service calls)
- Group by resource/feature
- Target: <300 lines per route file

**Step 4: Extract Utilities** (1 hour)
- Create `utils/` directory
- Move helper functions
- Add utility tests
- Target: <200 lines per utility file

**Step 5: Create App Factory** (1 hour)
- Create `app.py` with Flask app factory
- Move initialization logic
- Add configuration management
- Support testing and production modes

#### 1.3 Testing Strategy
- Unit tests for services (80%+ coverage)
- Integration tests for routes
- Mock external dependencies (Anthropic API, MCP)
- Use pytest + pytest-flask

### Phase 2: Frontend Modularization (Priority: MEDIUM)

#### 2.1 Project Structure
```
static/
├── index.html          # HTML shell only
├── css/
│   ├── theme.css
│   ├── layout.css
│   └── components.css
└── js/
    ├── main.js         # Entry point
    ├── config.js       # Configuration
    ├── api/            # Backend API calls
    │   ├── chat.js
    │   ├── sessions.js
    │   └── mcp.js
    ├── components/     # UI components
    │   ├── chat.js
    │   ├── sidebar.js
    │   ├── message.js
    │   └── toolbar.js
    ├── services/       # Business logic
    │   ├── markdown.js
    │   ├── syntax.js
    │   └── storage.js
    └── utils/          # Utilities
        ├── dom.js
        ├── theme.js
        └── validators.js
```

#### 2.2 Migration Steps

**Step 1: Extract CSS** (1 hour)
- Split CSS into theme, layout, components
- Use CSS variables for theming
- Remove inline styles

**Step 2: Extract Utilities** (2 hours)
- Create `utils/` directory
- Move DOM helpers, validators, formatters
- Target: <200 lines per file

**Step 3: Extract Services** (2-3 hours)
- Create `services/` directory
- Move markdown, syntax highlighting, storage
- Target: <300 lines per file

**Step 4: Extract API Layer** (2 hours)
- Create `api/` directory
- Move all fetch() calls
- Add error handling and retry logic
- Target: <200 lines per file

**Step 5: Extract Components** (3-4 hours)
- Create `components/` directory
- Move UI rendering logic
- Target: <400 lines per component

**Step 6: Module Bundler** (2 hours)
- Add build step (esbuild or rollup)
- Support ES modules
- Add source maps for debugging

#### 2.3 Testing Strategy
- Unit tests for utilities and services
- Component tests for UI
- E2E tests for critical flows
- Use Vitest or Jest

### Phase 3: Additional Improvements (Priority: LOW)

#### 3.1 Backend
- Add request/response logging middleware
- Add rate limiting
- Add API versioning
- Add OpenAPI/Swagger docs
- Add database migrations (Alembic)
- Add caching layer (Redis)

#### 3.2 Frontend
- Add TypeScript for type safety
- Add state management (if needed)
- Add lazy loading for routes
- Add service worker for offline support
- Add performance monitoring

#### 3.3 DevOps
- Add Docker support
- Add CI/CD pipeline
- Add staging environment
- Add monitoring and alerts

## Implementation Order

### Immediate (Next 2 weeks)
1. ✅ Backend Phase 1 (Steps 1-5)
   - Extract models, services, routes, utilities
   - Add unit tests
   - Maintain backward compatibility

### Short-term (Next 1 month)
2. Frontend Phase 2 (Steps 1-6)
   - Extract CSS, JS modules
   - Add build step
   - Add frontend tests

### Medium-term (Next 2-3 months)
3. Phase 3 improvements
   - TypeScript migration
   - Database migrations
   - Docker support
   - CI/CD pipeline

## Success Metrics

### Code Quality
- Backend files: <400 lines each
- Frontend files: <300 lines each
- Test coverage: >80%
- Zero circular dependencies

### Developer Experience
- New feature development time reduced by 50%
- Bug fix time reduced by 40%
- Onboarding time for new developers reduced by 60%

### Performance
- No regression in response times
- Faster frontend load times with bundling
- Better caching with modular structure

## Risk Mitigation

### Risks
1. Breaking existing functionality during refactor
2. User disruption during deployment
3. Increased deployment complexity with build steps

### Mitigation
1. Comprehensive test coverage before refactoring
2. Feature flags for gradual rollout
3. Maintain backward compatibility
4. Deploy during low-traffic periods
5. Have rollback plan ready

## Notes

- Start with backend (easier to test, less user-facing)
- Keep frontend working during backend refactor
- Use feature branches for each phase
- Review and test each step before merging
- Document changes in migration guide

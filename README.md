# UntitledCharts Backend
The backend API server for UntitledCharts, a community-driven chart hosting platform for Sonolus rhythm games.

## What Is This?

UntitledCharts is a chart hosting service for [Sonolus](https://sonolus.com/), a popular rhythm game platform where players can upload, share, and play custom charts (levels) created by the community.

### What Can Users Do?

- **Create an account** – Sign up using Discord authentication
- **Upload charts** – Share your custom rhythm game levels with the community
- **Browse charts** – Search and filter by title, artist, rating, tags, and more
- **Like & comment** – Interact with charts you enjoy
- **Staff picks** – Featured charts get highlighted by moderators

### Tech Stack

| Component        | Technology                                                | Purpose                                   |
|------------------|----------------------------------------------------------|-------------------------------------------|
| Web Framework    | [FastAPI](https://fastapi.tiangolo.com/)                 | Handles HTTP requests from the game       |
| Database         | [PostgreSQL](https://www.postgresql.org/)                | Stores users, charts, comments, scores    |
| File Storage     | [S3/R2](https://aws.amazon.com/s3/)                      | Stores chart files, images, music         |
| Authentication   | Discord OAuth                                            | Lets users log in with Discord            |
| Async Runtime    | [AsyncIO](https://docs.python.org/3/library/asyncio.html)| Handles many concurrent users             |

## Prerequisites

Before running this server, you'll need:

1. **PostgreSQL Database** (v14+ recommended)
    - Must have the `pg_cron` extension for scheduled tasks
    - Must have the `pg_trgm` extension for text search

2. **S3-Compatible Storage**
    - Amazon S3, Cloudflare R2, or any S3-compatible service
    - Stores: chart files, jacket images, music files, backgrounds

3. **Discord Application**
    - Create one at the [Discord Developer Portal](https://discord.com/developers/applications)
    - Required for OAuth login

### Python Requirements

```bash
# Python 3.10+

python --version # Should show 3.10 or higher

# Install dependencies
pip install -r requirements.txt
```

## Quick Start

### 1. Clone and Configure

```bash
# Clone the repository
git clone https://github.com/UntitledCharts/uc-chart-backend
cd uc-chart-backend

# Copy the example config
cp config.example.yml config.yml
```

### 2. Edit Configuration

Open `config.yml` and fill in your settings:

```yaml
server:
  port: 8000                              # Port the server runs on
  secret-key: "your-secret-key"           # Random string for session encryption
  base-url: "https://api.yourdomain.com/" # Public URL of this server
  sonolus-server-url: "https://your-sonolus-server.com/"
  sonolus-server-chart-prefix: "UC-"
  force-https: true
  auth: "your-auth-token"                 # Token shared with Sonolus frontend
  auth-header: "X-Custom-Auth"            # Custom header name (NOT "authorization")
  token-secret-key: "256bit-random-key"
  debug: false

s3:
  base-url: "https://your-bucket.r2.cloudflarestorage.com/"
  endpoint: "https://your-bucket.r2.cloudflarestorage.com/"
  bucket-name: "your-bucket-name"
  access-key-id: "your-access-key"
  secret-access-key: "your-secret-key"
  location:

psql:
  host: "localhost"
  user: "postgres"
  database: "untitledcharts"
  port: 5432
  password: "your-db-password"
  pool-min-size: 10
  pool-max-size: 20

discord:
  avatar-url: "https://cdn.discordapp.com/..."
  username: "UntitledCharts"
  published-webhook: "https://discord.com/api/webhooks/..."
  staff-pick-webhook: "https://discord.com/api/webhooks/..."
  all-visibility-changes-webhook: "https://discord.com/api/webhooks/..."
  comments-webhook: "https://discord.com/api/webhooks/..."

oauth:
  discord-client-id: "your-discord-client-id"
  discord-client-secret: "your-discord-client-secret"
  required-discord-server: 1234567890      # Your Discord server ID
```

### 3. Set Up the Database

```bash
# Run the database setup script
python -m scripts.database_setup
```

This creates all necessary tables:

- `accounts`      – User profiles and authentication
- `charts`        – Uploaded chart metadata
- `comments`      – User comments on charts
- `chart_likes`   – Likes on charts
- `leaderboards`  – Score submissions
- `notifications` – User notifications

### 4. Start the Server

```bash
# Run the main application
python main.py
```

The server will start on `http://localhost:8000` (or your configured port).

## Database Setup

### Installing PostgreSQL Cron Extension

The server uses `pg_cron` for scheduled tasks (like auto-publishing scheduled charts).

**Ubuntu/Debian:**

```bash
# Install the extension
sudo apt install postgresql-XX-cron # Replace XX with your PostgreSQL version

# Configure PostgreSQL
sudo nano /etc/postgresql/XX/main/postgresql.conf

# Find and modify these lines:
shared_preload_libraries = 'pg_cron'
cron.database_name = 'your_database_name'

# Restart PostgreSQL
sudo systemctl restart postgresql

# Connect to your database and enable the extension
sudo -i -u postgres
psql -d your_database_name

CREATE EXTENSION pg_cron;
```

### Database Schema

| Table         | Columns                                                                                                  | Relationships / Notes                                                        |
|---------------|----------------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `accounts`    | sonolus_id, sonolus_handle, sonolus_username, discord_id, mod, admin, banned                             | sonolus_id referenced as author in charts                                    |
| `charts`      | id, author, title, artists, status, like_count, comment_count, rating (1-5), staff_pick                  | author references accounts.sonolus_id; id referenced by chart_likes, comments|
| `chart_likes` | chart_id, sonolus_id                                                                                    | chart_id references charts.id; sonolus_id references accounts.sonolus_id     |
| `comments`    | chart_id, commenter, content                                                                            | chart_id references charts.id; commenter references accounts.sonolus_id      |

## Development

### Running in Debug Mode

```yaml
# In config.yml
server:
  debug: true
```

Debug mode enables:

- API documentation at `/docs`
- Detailed error messages
- Disables certain security restrictions

### Testing

```bash
# Install test dependencies
pip install pytest pytest-asyncio httpx

# Run tests
pytest
```

## Deployment

### Recommended Setup

1. **Reverse Proxy**: Use Nginx or Cloudflare in front
2. **Process Manager**: Use systemd or supervisor
3. **SSL**: Let's Encrypt for free certificates
4. **Backups**: Daily automated PostgreSQL backups

## Troubleshooting

### Common Issues

**"Connection refused" errors**
- Check PostgreSQL is running and accessible
- Verify credentials in `config.yml`

## License

The UntitledCharts backend is licensed under the **MIT/Expat** license. See [LICENSE](./LICENSE) for details.

## Links

- [Sonolus Official Site](https://sonolus.com/)
- [UntitledCharts Website](https://untitledcharts.com/)
- [Discord Community](https://discord.gg/UntitledCharts)

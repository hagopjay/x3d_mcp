# Running the playground

For the owner. Everything here happens on the admin page (`/admin`, linked from
the playground header when you are an admin) unless it says otherwise. Changes
take effect within ten seconds and need no redeploy.

## Your own way in

The deploy creates one owner pass. Its link was sent to you privately; keep it
like a password. It signs you in as an admin for 12 hours each time you open
it. If it leaks, make yourself a new admin pass on the admin page, sign in with
it, and revoke the old one.

## Letting people in

There are two ways, and you can use both.

### Pass links (works today)

Under **Passes**:

1. Type who it is for (shown only to you), optionally their address and a note.
2. Choose how many days it lasts (0 = no expiry), and optionally its own runs
   per hour and per day. Tick **admin** only for someone who should see this page.
3. **Create a pass link**, then **copy** and send it privately.

**Revoke** stops a link at once: the next thing that person does is refused and
they are signed out. Every pass shows its uses and when it was last used.

### Sign in with Google, by address or by domain

1. Create an OAuth client of type *Web application* in your cloud console
   (APIs and Services, Credentials). Add the playground's address as an
   **Authorized JavaScript origin**. No redirect URI is needed.
2. Paste the client ID into **Google OAuth client ID** and **Save who gets in**.
   The sign-in page now shows a Google button.
3. Fill the list:

| Box | One per line | Effect |
|---|---|---|
| **Domains** | `vt.edu` | Admits any verified address at `vt.edu` or a subdomain such as `cs.vt.edu`, and Google Workspace accounts of that domain. Not `notvt.edu`. |
| **Addresses** | `someone@gmail.com` | Admits that exact address |
| **Blocked** | `someone@vt.edu` or `spam.example` | Always refused, checked before everything else |
| **Admins** | `you@example.org` | Admits the address and opens this page to it |

Removing a domain or adding a block applies to people already signed in, within
ten seconds.

## Limits, spend and the pause switch

**Live** at the top shows this hour's and today's runs, spend today and in this
phase against the caps, and a 48-hour chart of runs. The **pause** switch stops
every model call at once; validation keeps working.

Under **Limits and writers** you can change any limit:

| Limit | Default |
|---|---|
| Runs per person per hour / per day | 12 / 40 |
| Runs per network per hour | 30 |
| Runs per day, everyone | 400 |
| Model spend per day / per phase (USD) | 3 / 10 |
| Validations per person per hour | 120 |
| Prompt / scene size (characters) | 1,200 / 200,000 |
| Model repairs per run | 3 |
| Run deadline (seconds) | 240 |

Spend is estimated from token counts at rounded-up prices, so the caps trip a
little early. Once a cap is reached, new runs are refused and running loops
stop before their next model call. The phase total survives restarts; it is
rebuilt from the run log.

**Writers switched on** turns individual writers off, for example one that is
misbehaving or too expensive.

## Watching what happens

- **Writers: how the loop is doing**: per writer, runs, first drafts that
  passed, final scenes that passed, repairs by the model and by the validator,
  average time and estimated cost.
- **Recent runs**: the last 60: the browser's thumbnail, when, who, writer, mode, first and final
  verdicts, repairs, whether the judge agreed, the browser's verdict, cost,
  time and the prompt.
- **People**: everyone who has signed in, how, when, and how many runs.
- **Access and abuse log**: sign-ins, refused passes and addresses, rate
  limits, revoked sessions and your own changes. **Only refusals** filters it.

## The run log

Every run, every browser verdict and every change is a row in the BigQuery
table `x3d_library.playground_runs`, partitioned by day. Some useful queries:

```sql
-- Runs by writer: first draft vs after the loop
SELECT writer, COUNT(*) AS runs,
       COUNTIF(first_verdict = 'pass') AS first_pass,
       COUNTIF(final_verdict = 'pass') AS final_pass,
       ROUND(SUM(usd_est), 4) AS usd
FROM x3d_library.playground_runs
WHERE row_kind = 'run'
GROUP BY writer ORDER BY runs DESC;

-- How often a judge alone agreed with the validator, and what it missed
SELECT judge, COUNT(*) AS judged, COUNTIF(judge_agree) AS agreed,
       SUM(judge_caught) AS caught, SUM(judge_missed) AS missed
FROM x3d_library.playground_runs
WHERE row_kind = 'run' AND judge IS NOT NULL
GROUP BY judge;

-- Valid but not visible: scenes only the browser caught
SELECT r.run_id, r.prompt, v.visible_status, v.coverage
FROM x3d_library.playground_runs r
JOIN x3d_library.playground_runs v USING (run_id)
WHERE r.row_kind = 'run' AND v.row_kind = 'visible'
  AND r.final_verdict = 'pass' AND v.visible_status != 'ok';

-- Spend this phase
SELECT ROUND(SUM(usd_est), 4) FROM x3d_library.playground_runs
WHERE row_kind = 'run' AND phase = '2026-10';
```

Run rows identify people by a keyed hash, never by address. The `state` rows
are the service's own journal (the list, passes, people, the abuse log) and do
contain addresses; they exist so the admin page survives restarts.

## Starting a new phase

Set `PHASE` when deploying (see [ARCHITECTURE.md](ARCHITECTURE.md#deploying)).
The phase cap then starts from zero for the new phase; old rows keep their
phase label.

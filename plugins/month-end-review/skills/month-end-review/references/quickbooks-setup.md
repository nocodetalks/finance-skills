# Connect QuickBooks Online (one time)

The skill reads your books with its own script, straight into files on your computer. You give it two keys from
your own Intuit developer account, then sign in to QuickBooks once in your browser. Nobody else's app is involved,
and the keys never go into the chat.

**You'll need:**
- a QuickBooks Online login that is an admin of the company
- about 20 minutes, plus however long Intuit takes to approve your production keys (part 1, step 5)

## Part 1: get your Client ID and Client Secret (Intuit developer site)

1. **Sign in.** Go to [developer.intuit.com](https://developer.intuit.com) and sign in with your Intuit account,
   or create one. Intuit asks for some basic company details the first time.
2. **Create the app.**
   1. Open **My Hub → App dashboard** (top right).
   2. Click the card with the **+**.
   3. Follow the steps: pick **QuickBooks Online**, name it something like "Month-end review", and tick **only the
      Accounting scope** (`com.intuit.quickbooks.accounting`).
3. **Add the return address.** Intuit sends your browser here after you sign in.
   1. Open your app, then **Settings → Redirect URIs**.
   2. Choose **Production**.
   3. Click **Add URI**, paste the address below, and save:
      ```
      https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl
      ```
   For an Intuit test company, add the same address under **Development**. It's often there already.
4. **Open the production keys.** Go to **Keys and credentials → Production**. The first time, Intuit shows its
   production key questionnaire.
5. **Fill in the questionnaire.** Answer it truthfully. Intuit requires it for every app that reads a real
   company, private ones included. For this skill:
   - **What the app does:** reads your own company's accounting data to review the month-end close. It's used only
     by your company.
   - **Writing:** it never writes to QuickBooks.
   - **Where data is stored:** only on your own computer.
   - **Links Intuit asks for** (terms, privacy policy, website): use your company website's pages.

   Intuit reviews the answers before it shows the production keys, and may also ask you to complete a yearly
   security review. Until the keys appear, the skill can't read your real books.
6. **Copy the keys.** Turn on **Show credentials**. Keep the page open so you can copy the **Client ID** and
   **Client Secret** in part 2.

Test company instead? Use **Keys and credentials → Development**. These keys need no questionnaire, but they only
work with Intuit test companies, never your real books.

## Part 2: put the keys in the `.env` file (not the chat)

1. **The assistant creates the file and opens it for you.** It runs `mer.py connect --environment production`.
   The first time, this creates `month-end/.env`, readable only by you, and opens it in a text editor:
   ```
   QUICKBOOKS_CLIENT_ID=
   QUICKBOOKS_CLIENT_SECRET=
   STRIPE_API_KEY=
   ```
   `STRIPE_API_KEY` is optional; see [stripe-setup.md](stripe-setup.md).
2. **Paste the keys.** Paste each one right after its `=` sign, with no spaces, then save the file.
3. **Say "done" in the chat.** Don't paste the keys into the chat. The assistant never opens this file; only the
   connect script reads it.

## Part 3: sign in to QuickBooks (2 minutes)

1. **Sign-in page.** The assistant runs `connect` again, and Intuit's sign-in page opens in your browser.
2. **Connect.** Sign in, choose the company to review, and click **Connect**.
3. **Copy the address.** The browser lands on one of two Intuit pages:
   - Intuit's **OAuth Playground**, showing an **Authorization Code** and a **Realm ID**
   - an **Intuit sign-in page**

   Don't click **Get tokens** and don't sign in: either uses up the code. Copy the whole address from the address
   bar and paste it into the chat within a few minutes. On the Playground page, copying the Authorization Code
   and Realm ID works too.

   This is safe to paste. The code works only once, only together with your Client Secret (which stays in
   `.env`), and it expires within minutes.
4. **Check the company.** The assistant finishes the connection and shows the company's name. Check it's the
   right one.

The connection is saved in `~/.config/finance-skills/quickbooks-default.json`, readable only by you. Run
`mer.py connect --check` any time to confirm it still works. It shows the company, never the keys.

## What "read-only" means here

Intuit's Accounting permission can't be limited to reading, so the guarantee is in this skill's code.
`scripts/mer/qbo.py` sends only `GET` requests (reports and queries). It sends one `POST`, to Intuit's token
service, to swap the sign-in code or the saved token for a fresh access token. There is no code that creates,
changes or deletes anything in QuickBooks.

## Good to know

- **More than one company.** Give each its own profile:
  - connect with `mer.py connect --profile acme --environment production`
  - pull with `mer.py pull-qbo --profile acme ...`

  One set of keys can connect many companies.
- **One connection per tool.** Don't copy this connection into another tool (Intuit's QuickBooks MCP server, for
  example). Intuit replaces the saved token as it's used, so two tools sharing one would lock each other out.
- **Reconnecting.** If the connection is unused for about 100 days, or is disconnected in QuickBooks, `pull-qbo`
  stops with "refused the saved connection". Run part 3 again; the keys in `.env` are still there.
- **Disconnecting.**
  - Remove the app from your QuickBooks company's connected apps.
  - Delete `~/.config/finance-skills/quickbooks-<profile>.json`.
  - Delete `month-end/.env` if you don't need the keys any more.
- **Keep `month-end/` out of git.** When `connect` creates the folder, it adds a `.gitignore` that ignores
  everything in it.

## If something goes wrong

| What you see | What it means |
|---|---|
| An Intuit error page right after clicking Connect, mentioning the redirect URI | The return address in part 1, step 3 is missing from the **Production** (or Development) list. Add it and run connect again |
| "Intuit didn't accept the Client ID and Secret" | The keys are from the wrong tab (Development vs Production), or something extra was copied. Paste them again into `.env` |
| "That's Intuit's company-picker page" | The address was copied before clicking Connect. Choose the company, click Connect, then copy the next page's address |
| "the sign-in code has expired or was already used" | Too much time passed, **Get tokens** was clicked on the Playground page, or the address was used already. Run connect again and paste the newest address quickly |
| "access_denied" | Connect wasn't clicked, or this login isn't allowed to connect apps to the company. Use an admin login |
| "already connected to another company" | That profile has a different company. Use `--profile <new name>` to keep both, or `--replace` |

# Build log

What I tried, what broke, and what I changed, in the order it happened.

## Before building: talking to our CS manager
- My first idea was to scrape the prospect's website and send the rep a summary.
- When I asked how she preps, she said she wanted template ideas to show prospects. With 800+ templates, when one doesn't come to mind, she can spend a long time searching for a fit or using AI to come up with ideas.
- Prep takes her 10-25 minutes per demo, with up to 6 demos on a busy day.
- That changed the main output from a website summary to template recommendations.

## Fetch step
- requests + BeautifulSoup got ~4,100 characters from nicholastrigili.com. No About page linked.
- Stat blocks lost their numbers ("10,000 / Clients coached / Years of experience / Audience online"), and a slider of 18 client names came through as noise.

## Bug: ThinkingBlock has no attribute 'text'
- The code read only the first block of Claude's reply. The model thinks first, so the first block was thinking, not the answer.
- Fix: keep only the text blocks. Raised the reply limit so thinking can't cut the answer off. Added a token count to every run.

## Prompt v1 (nicholastrigili.com): 1,823 tokens in, 2,571 out
What worked:
- Used the prospect's note ("more qualified applicants") and noticed every CTA is "Apply".
- Noticed the blank stat numbers instead of making them up.
- Noticed the booking looked like test data.

What broke:
- Too long. Ten sections, nowhere near a 2-minute skim.
- Invented quiz ideas instead of recommending real templates. No links.
- Guessed at things the site doesn't say: "Men roughly 30-55... with disposable income."
- Wrote for someone building the quiz, not a rep prepping for a call: pitch line, feature list, compliance notes, segmentation fields.
- Markdown output, which won't format cleanly in Slack.

## Prompt v2: changes
- Says who the reader is (a CS rep skimming in Slack before the call).
- Gives Claude the template library and allows only those templates.
- Every claim needs a word-for-word quote; otherwise "Unknown". No guessing demographics.
- Discovery questions must be tied to something on the site or in the note.
- Explicitly leaves out pitch lines, feature lists, compliance, and quiz-building advice.
- Returns JSON so the code can check it.
- Code now checks: templates must exist in templates.json (others removed), and every quote must actually appear on the site or in the note (otherwise marked UNVERIFIED).

## Prompt v2 (nicholastrigili.com): 8,548 tokens in, 2,612 out
- Real templates with links, every claim has a quote, no demographic guessing, no extra sections.
- Input went from 1,823 to 8,548 tokens. Most of that is the template library (40 templates), not the website.
- Output is about the same size as v1 even though the brief is much shorter, so most of the output is the model thinking before it answers.
- The quote check caught a real problem: Claude quoted the note as "get us more qualified applicants". The note actually says "help us get more qualified applicants". It looked like a quote but was a paraphrase, and it was reused in a template "why". This is why the code checks quotes instead of trusting the prompt.

## Prompt v3: fix the paraphrased quote
- Prompt: quotes must be copied character for character, with no rewording or grammar fixes. Any words in quotation marks inside a template "why" must also be exact.
- Code: the quote check now also covers quotes inside template "why" lines (it missed the paraphrase there in v2).
- Code: the template library is sent one template per line without indentation. That saved only about 7%, because the descriptions are most of the size.
- v2 stays in prompts/ to show what changed.

## Prompt v3 result (nicholastrigili.com): 8,608 tokens in, 2,327 out
- Clean run: no unverified quotes, no removed templates. Three real templates, four questions tied to the site or note.

## Slack posting
- Without --dry-run, the brief posts to #demo-prep through the webhook as Block Kit: header, prospect + note, what they do (with quotes), audience, templates (linked), discovery questions, flags.
- Unverified quotes show a warning icon in Slack, so the rep sees them.
- v1 output (free text) posts as plain text so v1 vs v3 can be compared in Slack.

## Calendly connection (--watch)
- Free Calendly plan has no webhooks, so the agent polls the API every 2 minutes for upcoming bookings on the test account.
- For each new invitee it reads name, email, and the form answers ("Your Website URL" and the optional prep note), then runs the same pipeline and posts to Slack.
- Handled bookings are saved in seen_bookings.json so nothing is prepped twice.
- In production this would be the company Calendly's "invitee created" webhook instead of polling.

## First real Calendly run (v3): 3 bookings, all posted to Slack
Sites: amyporterfield.com (online marketing educator), bloomiesstudio.com (local florist), cowbell.insure (cyber insurance)

What I didn't like:
- Quotes under every claim repeated the claim almost word for word. Too much to read.
  Example: "Locally owned floral design studio..." / "locally owned, woman-led floral design studio"
- Unhelpful flag: the meta description names the podcast "Online Marketing Made Easy" but the page calls it "The Amy Porterfield Show". A CS rep doesn't need that.
- Forced template matches: for a florist it recommended "What type of cupcake are you?" The cause was my own v3 rule: "Recommend 2 or 3 templates... Do not invent templates or quiz ideas." With no good match, it had to pick something.

## Prompt v4: changes
- Quotes are still required, but only used by the code to check claims. The rep only sees a quote when the check fails.
- Templates only when they're a strong match (0 to 3, none is fine). Otherwise suggest quiz ideas (title + why) so the rep still has 2 or 3 starting points.
- Flags only for things that change how the rep handles the call (unreadable site, fake booking, site/note mismatch, possible existing customer or competitor). Missing info becomes a discovery question instead.
- Code: quiz ideas get the same quote check; new "Quiz ideas" section in Slack.

## Prompt v4 result (same 3 Calendly bookings): ~8,800-9,000 tokens in, ~2,300-2,800 out
- Florist: no template recommended ("No close match in the template library"), three flower-specific quiz ideas (bouquet style, arrangement size, wedding florals). The cupcake problem is gone.
- Amy Porterfield: two real templates that fit an online course creator, plus one quiz idea tied to her "3 Funnels" offer. The podcast-name flag is gone.
- Cowbell: one template (business risk level, built for insurance), two quiz ideas.
- No quotes under claims; no quote-check warnings.
- Only one flag across three briefs: the booker's name and Gmail don't match Amy Porterfield's business. That's a real flag under the new rule (it comes from testing with my own name).
- The messy URL "cowbell.insure/" (no https) was cleaned correctly.
- Tweak to v4: removed the "booking looks fake" flag and told it not to flag the booker's name or email. People often book with a personal email or for someone else's business, so it isn't useful to the rep.

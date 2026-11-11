# Automatic book, evening pass for Tuesday 2026-11-10

Written late on Monday 2026-11-09. 20 candidates judged: 2 takes, 18 passes. No own ideas added.

## The tape

SPY (S&P 500 ETF) +2.4% on Monday, its best day since April, a rebound from the 11-05 AI rout led by chips (chip index +6.9%) as oil fell on Iran talk headlines (WTI settled 86.85, -4.4%). Late evening from IBKR, delayed: ES 7,581 (+0.1%), NQ -0.1%, VIX 19.92 (from 25.64), 10-year 4.86%, WTI 85.84.

Inside the week: CPI Tuesday 8:30 AM ET, before the open; CoreWeave Tuesday after the close; bond market closed and the House reply on Nvidia due Wednesday; OPEC report Wednesday, IEA Friday; Cisco and Applied Materials Thursday after the close; 10- and 30-year auctions; PPI Friday.

## How the screen was built

The 7:00 PM ET nightly job failed at its first step (`git pull`: "Could not resolve host: github.com"), so it wrote no brief, scan or screen and made no commit. I ran `uv run fr screen` by hand at about 9:20 PM ET from the 4:30 PM ET bars (311 symbols, settled). Earnings dates are from the 11:05 AM ET pull. The screen showed no report date for FLY, which reports Tuesday after the close.

## Post-mortem

**2026-11-09-D08, short RDW (Redwire) against XLI (industrials ETF): noise.** Stopped on session 1, down 10.4%. RDW rose about 12% with the whole space group on NASA's lunar delivery awards and the Firefly-AST engine deal, on a +2.4% market day; no Redwire news, and the revenue miss behind the short still stands. About 18.6% of the float was short and XLI doesn't move with space stocks. One trade, so nothing added to `lessons.md`.

## Takes

### P01 Short NVDA (Nvidia) / long QQQ (Nasdaq 100 ETF), conviction 3, 4 sessions, stop 7%
- **What changed:** -4.3% to 178.67 on 3.9x volume while SPY rose 2.4% and chips 6.9%; down 8.8% at the low; under its 20-day low; -21% in 20 days.
- **Story:** the board opened an independent investigation on 11-08 after NYT and FT reports that US intelligence believes a Megaspeed executive passed information to Beijing and targeted the CEO, who admitted an affair and stays in the job. On 11-09 the NYT said the Megaspeed chip-diversion probe moved to the Justice Department as a criminal inquiry; Trump said the FBI is looking into Huang over "something else"; a class action is reported. These are unproven allegations. The first report was about 10-29.
- **Chart:** for, the heaviest relative volume on the screen and money going to AMD and Broadcom instead. Against, RSI 27, 3.6 ATR under the 20-day, 8% above the 52-week low, and half the intraday fall was bought back.
- **Coming:** House reply 11-11; results 11-17 after the close. The limit exits Friday 11-13.
- **Case against:** an oversold stock where one clearing statement reverses the move; its own history in this setup is thin with one very large loss.
- **Challenge:** no disqualifier. It showed SMH (semiconductor ETF), the screen's hedge, buys back part of the short because NVDA is a top holding, so the hedge was changed to QQQ.
- **Wrong if:** the board, the Justice Department or the FBI clear Huang, the House reply closes the matter, or Huang is confirmed on the Shenzhen trip.

### P02 Short seat-based software / long IGV (software ETF), conviction 3, 5 sessions, stop 6%
Short CRM, NOW, WDAY, ADBE, TEAM, HUBS, MNDY, ASAN in equal weights.
- **What changed:** -4.1% against IGV on Monday, -8.7% in five days, a 20-day relative low; 1 of 8 above its 50-day.
- **Story:** HubSpot (11-04), Atlassian (11-05) and monday.com (11-09) all beat and all guided growth lower, after ServiceNow on 10-28. Canaccord cut monday.com to Hold on 11-09. HubSpot named AI headwinds to demand.
- **Chart:** for, a broad relative low on a strong market day. Against, Atlassian and ServiceNow are deeply stretched, HubSpot rose 2.7% Monday, and monday.com was about a quarter of Monday's move.
- **Coming:** no member reports in the window; ServiceNow at a BMO fireside chat 11-12, 10:00 AM ET.
- **Case against:** it follows an 8.7% move with no new catalyst ahead, and IGV holds four of the eight names.
- **Challenge:** no disqualifier; the two earliest names have steadied, the basket has not.
- **Wrong if:** ServiceNow raises or firmly reaffirms its outlook on 11-12, two or more members are upgraded on seat growth, or the basket wins back its five-day loss on no news.

## Passes

- **SHLS (Shoals), 1:** trades on its fixed merger ratio with Array, which fell 11%.
- **USO (oil fund), 2:** a one-way call on Iran diplomacy, with attacks continuing.
- **TEAM (Atlassian), 2:** +0.8% on a +2.4% day isn't a turn; it's a leg of P02.
- **CAT (Caterpillar), 2:** group rebound, company story unchanged.
- **MTUM (momentum ETF), with XLK, IWF, QQQ, SMH, 2:** one rebound day, CPI before the open.
- **MPC (Marathon Petroleum), 2:** strong quarter but diesel margins are falling with each peace headline. Closest of the longs.
- **CSCO (Cisco), 2:** a run into Thursday's report behind a downgrade.
- **Managed care, 2:** no story found.
- **Chip equipment, 2:** partly NVDA falling inside SMH, so the same bet as P01; Applied Materials reports Thursday.
- **AI networking and optics, 2:** same mechanical lift; no named cause.
- **FLY (Firefly), 1:** reports Tuesday after the close after +49% in five days.
- **MTUM/USMV, SPHB/SPLV, 2 each:** the same one-day rebound.
- **HYG/IEF (high yield against Treasuries), 2:** a 0.4% day ahead of CPI and auctions.
- **CVI (CVR Energy), 2:** no turn; fell on a $750M share sale and a large acquisition.
- **MCD (McDonald's), 1:** no turn, targets being raised.
- **NOW (ServiceNow), 2, FTNT (Fortinet), 2:** stretched, no turn, stories unchanged.

## Not verified

- NVDA: the FT and NYT originals (read through CNBC and Yahoo), the class action filing, and that no rating changed overnight.
- Seat-based software: monday.com's closing move, its Q4 consensus, the Canaccord cut and the BMO chat time each rest on one secondary source; the challenge agent couldn't confirm them.
- Short interest for every name is weeks or months old.
- Why managed care fell; Monday's moves for several basket members.
- RDW: no source states a cause for Monday's rise beyond the group rally.

## Added mid-session at the user's request: 2026-11-10-A03, short NOVT against XLK (about 12:14 PM ET)

Entered with market orders after the 9:14 AM ET cutoff, not at the open, on the user's instruction; proposal 2026-11-10-P21, pass "manual". Conviction 3, up to 5 sessions, stop -8%. Fills: short 80 NOVT at 124.54, long 59 XLK at 167.95. The judgment came from a rehearsal pass on part-day prices.

- What changed: Q3 beat, but Q4 adj. EPS guide $0.91-0.96 against about $1.02 consensus (one source) and FY26 adj. EPS guide cut to $3.66-3.71 from $3.68-3.74. Down about 15% on heavy volume.
- Story: forward cut; net debt $689.4M after Riverpoint; $250M contingent milestone reported as payable Q1 2027.
- Chart: opened 134.00, low 120.01; 50-day under 200-day.
- Coming: management at Baird on 2026-11-11; no report inside the time limit.
- Case against: bookings +29%, backlog $543M, 2027 growth guided above 15%; short interest about 14% of float, 11.5 days to cover; resembles the RDW loss; the book is also short CGNX.
- Wrong if: management or an analyst shows the Q4 shortfall is a quantified one-off, or credibly frames the guide as conservative.
- Could not verify: the UK production disruption (one article, no transcript); the milestone's exact date.

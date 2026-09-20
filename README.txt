PriceFx weekly approved price list report
=========================================

WHAT IT DOES
  Asks PriceFx for every price list submitted inside one Sunday-to-Saturday
  week, keeps the ones whose workflow status counts, then for each one runs the
  same Summary/Calculate the screen runs, and writes a CSV with your columns
  plus the vendors whose SKU Impact is beyond +/-100,000.

  It reads. It never writes anything back to PriceFx.

WHICH PRICE LISTS COUNT
  By default APPROVED and NO_APPROVAL_REQUIRED. Change that with
  workflow_statuses in pricefx_config.ini - spelling and case do not matter.

  Anything submitted in the week that did NOT count is named, with the status
  that excluded it, in price_lists_left_out.txt next to the report. That file is
  the point: a price list can no longer go missing quietly. If one of the
  statuses listed there should be counted, copy it into workflow_statuses.

SETUP  (once)
  1. pip install requests
  2. Copy pricefx_config.example.ini to pricefx_config.ini
  3. Put your password in it and check the output folder

  Your password stays in that file on your machine. Do not email it to anyone,
  me included.

RUNNING
  python pricefx_weekly.py --check
      Signs in, shows which statuses it treats as approved, lists the first few
      price lists it found, and confirms it can read SKU Impact. Writes nothing.
      RUN THIS FIRST.

  python pricefx_weekly.py
      The real thing. Does the last complete Sunday-to-Saturday week and writes
      the CSV.

  python pricefx_weekly.py --week 2026-09-20
      A specific week, given the Sunday it starts.

THE WEEK
  Run any day, it reports the last week that has FINISHED. Run on Sunday 27 Sep
  and you get Sunday 20 Sep 00:00:00 to Saturday 26 Sep 23:59:59.

  Seconds are included deliberately. 12:01am would leave a minute at midnight
  that belongs to no week at all, and a price list submitted in it would vanish.

FILES IT WRITES, BESIDES THE REPORT
  price_lists_left_out.txt   every price list in the week that did not count,
                             and the status that excluded it
  summary_columns.txt        what the Summary reply calls its columns, names
                             and types only - no figures, no vendor names

  Both exist so that if something looks wrong, the answer is already written
  down instead of costing a round of screenshots. Neither contains anything
  commercial, so either can be sent on.

NOTES FROM THE FIRST LIVE RUNS
  - The status filter used to run server-side, which meant a status I had not
    thought of returned nothing and left no trace. It now fetches the whole
    week and chooses in Python, so anything skipped gets named.

  - The vendor column is found by the shape of its value - the one piece of
    text in a row of numbers - rather than by guessing its name, because the
    reply does not call it what the request does.

  I have no PriceFx access and did not want yours, so the live calls are
  exercised by you and the logic is exercised here against a rebuilt copy of a
  week's replies.

PriceFx weekly approved price list report
=========================================

WHAT IT DOES
  Asks PriceFx for price lists that are approved AND were submitted inside one
  Sunday-to-Saturday week, then for each one runs the same Summary/Calculate
  the screen runs, and writes a CSV with your columns plus the vendors whose
  SKU Impact is beyond +/-100,000.

  It reads. It never writes anything back to PriceFx.

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

WHAT I COULD NOT TEST
  I have no PriceFx access and did not want yours, so the logic is tested but
  the live calls are not. Two things could need a small fix on first run:

  - the exact workflow status wording. Rather than assume "APPROVED", it asks
    PriceFx what its statuses are called and matches anything containing
    "approv". --check prints what it chose, so you can see it is right.

  - the column name for SKU Impact in the reply. The capture showed what the
    browser sends, not what comes back. It looks for any column whose name
    matches, and if it cannot find one it prints the columns it did get and
    tells you, rather than silently reporting zero impact.

  That second one is why --check exists. If it prints a list of columns and
  asks which is which, send me that line and it is a one-word fix.

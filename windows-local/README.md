# Price History Tracker (Amazon + Flipkart)

Tracks prices of Amazon (any Amazon country site, e.g. amazon.in) and Flipkart products over time, with a chart,
lowest/highest/average stats, a "good time to buy?" verdict, target-price alerts and CSV export.

Runs on built-in Windows PowerShell — nothing to install.

## Start

Double-click **Start-PriceTracker.bat**. Your browser opens the tracker (http://localhost:8765, or the next
free port if that one is busy). Keep the black window open while you use it; close it to stop.

Paste a product link (full links and short links like amzn.in / dl.flipkart.com both work), optionally set a
target price, and click **Track product**. Each time you click **Check now** / **Check all now**, the current
price is recorded.

## Automatic daily checks

History only builds up when prices are checked. To check every day automatically, run once in PowerShell
from this folder:

    .\Start-PriceTracker.bat -Schedule              # daily at 09:00
    .\Start-PriceTracker.bat -Schedule -ScheduleTime 20:30

This creates a Windows scheduled task. When a product drops to your target price you get a Windows notification.
Remove it with `.\Start-PriceTracker.bat -Unschedule`.

## Command line

    .\Start-PriceTracker.bat -Add "<product url>" -Target 49999
    .\Start-PriceTracker.bat -Check     # check all now
    .\Start-PriceTracker.bat -List      # print stats

## Files

- `data\products.json` — all products and their price history
- `data\check.log` — log of scheduled checks

## Limits

- History starts from the day you add a product; past prices can't be fetched retroactively.
- It reads the public product page. If Amazon or Flipkart show a bot-check page or change their layout, that
  check fails and the error is shown on the product; just try again later. Checks are spaced a few seconds
  apart to stay polite.

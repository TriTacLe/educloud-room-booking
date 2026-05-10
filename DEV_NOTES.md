#### Solution

Using Playwright browser automation and not HTTP requests

Playwright: lib that controls real browser (Chromium) from code (like e2e testing or remote controlling). Can click btns, fill forms, read page content, intercept network req.

Playwright logs in, opens Chronium headed, navigates to TP. User do MFA, scripts waits. Once user land on booking page, it saves cookies + localStorage to data/storage_state.json

Then book: loads saved cookies into new browser context because browser is already authenticated, reusing the result - session restored from saved state. Then playwright controls browser

A little different for cancel, navigates too /my-bookings, intercepts bookings.php XHR to get booking list, then calls DELETE for each booking using page.evaluate() (runs JS inside the browser that makes the fetch). In hindsight i could have used this apporach from the start besides login.

Why this approach, browser automation instead of HTTP? TP has no public API. Login goes through microsoft entra with MFA and then its not possible to automate. Browser automation lets user to MFA and the script then automates the logged in browser.

#### Analyzing the problem

TP has react SPA, when GET booking page, it loads mostly empty HTML shell and JS files. The data comes from XHR calls to php endpoints (.php in devtools) that js makes after page loads. Strategy is to replicate those XHR calls. This is easy but authentication is the problem. Solution is for the user to manually authenticate during MFA. (this can also be automated but for my purpose its overkill)

For auth php endpoints requires valid session cookie. To get that cookie user has to complete full login: tp -> feide -> NTNU idp -> entra (OIDC) -> mfa -> redir back. In theory microsoft entra with number MFA cannot be automated and requires human actions but i have brainstormed and un tested solution: could remote access control via wifi. Maybe tailscal Maybe tailscale

The downside is that user (human) has to do MFA in browser and enter their credentials in login prompt. The script captures cookies, saves in a file and reuse them for all future req. In retrospective the cookie is just a string and i could have made XHR calls using HTTP req instead of playwright. The browser automation is only needed for login.

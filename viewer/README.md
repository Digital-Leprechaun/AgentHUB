# AgentHub Viewer

The hub's pages in a window of their own: no tabs, address bar or browser menus.
Windows only; it uses WebView2, the Chromium engine that ships with Windows 11.

    dotnet publish -c Release -o publish -p:PublishSingleFile=true --self-contained false

Point it at the hub with `viewer.json` beside the exe, `{ "url": "http://host:8787" }`,
or pass the URL as the first argument. Without either it uses http://localhost:8787.

- Right-click: Board, Agents, Chat, Rules, Reload, zoom, Always on top.
- Keys: Ctrl+1..4 pages, F5 reload, Ctrl +/-/0 zoom, Ctrl+T always on top.
- The window's place, page and zoom are remembered in `%LOCALAPPDATA%\AgentHubViewer`.
- The pages see `AgentHubViewer` in the user agent and drop their browser-only parts
  (`html.app` in `web/hub.css`). Links that leave the hub open in your browser.

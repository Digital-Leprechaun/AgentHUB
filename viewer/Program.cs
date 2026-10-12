// AgentHub Viewer: the hub's web pages in a window of their own, with no browser
// around them. The pages are the hub's own (WebView2, the Chromium engine Windows
// ships); the viewer adds only a window that remembers where it was, and a right-click
// menu for the pages, reload, zoom and always-on-top.
//
// The hub's address comes from, in order: the first command-line argument, viewer.json
// beside the exe ({"url": "http://host:8787"}), or http://localhost:8787. The window's
// place, page and zoom are kept in %LOCALAPPDATA%\AgentHubViewer\state.json.

using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace AgentHubViewer;

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new ViewerForm(HubUrl(args)));
    }

    static string HubUrl(string[] args)
    {
        if (args.Length > 0 && Uri.TryCreate(args[0], UriKind.Absolute, out var u)) return u.ToString().TrimEnd('/');
        var cfg = Path.Combine(AppContext.BaseDirectory, "viewer.json");
        try
        {
            if (File.Exists(cfg))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cfg));
                if (doc.RootElement.TryGetProperty("url", out var v) && v.GetString() is { Length: > 0 } s)
                    return s.TrimEnd('/');
            }
        }
        catch (Exception e)
        {
            MessageBox.Show($"Cannot read {cfg}: {e.Message}", "AgentHub Viewer");
        }
        return "http://localhost:8787";
    }
}

sealed class State
{
    public int X { get; set; } = -1;
    public int Y { get; set; } = -1;
    public int W { get; set; } = 900;
    public int H { get; set; } = 700;
    public bool Maximized { get; set; }
    public bool OnTop { get; set; }
    public string Page { get; set; } = "/board";
    public double Zoom { get; set; } = 1.0;

    static readonly string Dir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AgentHubViewer");
    static string FilePath => Path.Combine(Dir, "state.json");

    public static State Load()
    {
        try { return JsonSerializer.Deserialize<State>(File.ReadAllText(FilePath)) ?? new State(); }
        catch { return new State(); }
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Dir);
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this));
        }
        catch { /* a lost window position is not worth an error */ }
    }

    public static string UserDataFolder => Path.Combine(Dir, "WebView2");
}

sealed class ViewerForm : Form
{
    static readonly (string Name, string Path, Keys Key)[] Pages =
    {
        ("Board", "/board", Keys.Control | Keys.D1),
        ("Agents", "/agents", Keys.Control | Keys.D2),
        ("Chat", "/", Keys.Control | Keys.D3),
        ("Rules", "/rules", Keys.Control | Keys.D4),
    };

    readonly string hub;
    readonly State state = State.Load();
    readonly WebView2 web = new() { Dock = DockStyle.Fill };

    public ViewerForm(string hub)
    {
        this.hub = hub;
        Text = "AgentHub";
        BackColor = Color.FromArgb(0x14, 0x16, 0x1a);  // the pages' --bg, so no white flash
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        KeyPreview = true;
        TopMost = state.OnTop;

        StartPosition = FormStartPosition.Manual;
        var bounds = new Rectangle(state.X, state.Y, state.W, state.H);
        if (state.X < 0 || !Screen.AllScreens.Any(s => s.WorkingArea.IntersectsWith(bounds)))
            bounds = new Rectangle(Screen.PrimaryScreen!.WorkingArea.Location + new Size(40, 40), bounds.Size);
        Bounds = bounds;
        if (state.Maximized) WindowState = FormWindowState.Maximized;

        Controls.Add(web);
        Load += async (_, _) => await StartAsync();
        FormClosing += (_, _) => SaveState();
    }

    async Task StartAsync()
    {
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(null, State.UserDataFolder);
            await web.EnsureCoreWebView2Async(env);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            MessageBox.Show("The Microsoft Edge WebView2 Runtime is not installed.", "AgentHub Viewer");
            Close();
            return;
        }

        var cw = web.CoreWebView2;
        // The pages see this and drop their browser-only parts (hub.js: html.app).
        cw.Settings.UserAgent += " AgentHubViewer";
        cw.Settings.AreBrowserAcceleratorKeysEnabled = false;  // no Ctrl+P, Ctrl+F, devtools keys
        cw.Settings.IsStatusBarEnabled = false;
        cw.Settings.IsGeneralAutofillEnabled = false;
        cw.Settings.IsPasswordAutosaveEnabled = false;
        web.ZoomFactor = state.Zoom;

        // Links that leave the hub open in the real browser, not in this window.
        cw.NewWindowRequested += (_, e) => { e.Handled = true; OpenOutside(e.Uri); };
        cw.NavigationStarting += (_, e) =>
        {
            if (!e.Uri.StartsWith(hub, StringComparison.OrdinalIgnoreCase)) { e.Cancel = true; OpenOutside(e.Uri); }
        };
        cw.DocumentTitleChanged += (_, _) => Text = cw.DocumentTitle;
        cw.SourceChanged += (_, _) =>
        {
            if (Uri.TryCreate(cw.Source, UriKind.Absolute, out var u)) state.Page = u.PathAndQuery;
        };
        cw.ContextMenuRequested += OnContextMenu;

        web.Source = new Uri(hub + (state.Page.StartsWith('/') ? state.Page : "/board"));
    }

    void OnContextMenu(object? sender, CoreWebView2ContextMenuRequestedEventArgs e)
    {
        var cw = web.CoreWebView2;
        var items = e.MenuItems;
        // Keep the text-editing items (copy, paste, ...) and drop the browser ones.
        for (int i = items.Count - 1; i >= 0; i--)
            if (items[i].Name is not ("copy" or "cut" or "paste" or "selectAll" or "pasteAndMatchStyle"))
                items.RemoveAt(i);
        if (items.Count > 0) items.Add(cw.Environment.CreateContextMenuItem("", null, CoreWebView2ContextMenuItemKind.Separator));

        foreach (var (name, path, _) in Pages)
        {
            var it = cw.Environment.CreateContextMenuItem(name, null, CoreWebView2ContextMenuItemKind.Command);
            it.CustomItemSelected += (_, _) => Go(path);
            items.Add(it);
        }
        items.Add(cw.Environment.CreateContextMenuItem("", null, CoreWebView2ContextMenuItemKind.Separator));
        Add(items, "Reload\tF5", () => cw.Reload());
        Add(items, "Zoom in\tCtrl +", () => ZoomBy(0.1));
        Add(items, "Zoom out\tCtrl -", () => ZoomBy(-0.1));
        Add(items, "Actual size\tCtrl 0", () => SetZoom(1.0));
        var top = cw.Environment.CreateContextMenuItem("Always on top\tCtrl T", null, CoreWebView2ContextMenuItemKind.CheckBox);
        top.IsChecked = TopMost;
        top.CustomItemSelected += (_, _) => ToggleTop();
        items.Add(top);
    }

    void Add(IList<CoreWebView2ContextMenuItem> items, string label, Action act)
    {
        var it = web.CoreWebView2.Environment.CreateContextMenuItem(label, null, CoreWebView2ContextMenuItemKind.Command);
        it.CustomItemSelected += (_, _) => act();
        items.Add(it);
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        if (web.CoreWebView2 is null) return base.ProcessCmdKey(ref msg, keyData);
        foreach (var (_, path, key) in Pages)
            if (keyData == key) { Go(path); return true; }
        switch (keyData)
        {
            case Keys.F5: web.CoreWebView2.Reload(); return true;
            case Keys.Control | Keys.T: ToggleTop(); return true;
            case Keys.Control | Keys.Oemplus: case Keys.Control | Keys.Add: ZoomBy(0.1); return true;
            case Keys.Control | Keys.OemMinus: case Keys.Control | Keys.Subtract: ZoomBy(-0.1); return true;
            case Keys.Control | Keys.D0: case Keys.Control | Keys.NumPad0: SetZoom(1.0); return true;
        }
        return base.ProcessCmdKey(ref msg, keyData);
    }

    void Go(string path) => web.CoreWebView2.Navigate(hub + path);
    void ZoomBy(double d) => SetZoom(web.ZoomFactor + d);
    void SetZoom(double z) { web.ZoomFactor = Math.Clamp(Math.Round(z, 1), 0.5, 2.0); state.Zoom = web.ZoomFactor; }
    void ToggleTop() { TopMost = !TopMost; state.OnTop = TopMost; }

    static void OpenOutside(string uri)
    {
        if (Uri.TryCreate(uri, UriKind.Absolute, out var u) && (u.Scheme == "http" || u.Scheme == "https"))
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(u.ToString()) { UseShellExecute = true });
    }

    void SaveState()
    {
        state.Maximized = WindowState == FormWindowState.Maximized;
        var b = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
        (state.X, state.Y, state.W, state.H) = (b.X, b.Y, b.Width, b.Height);
        state.Zoom = web.ZoomFactor;
        state.Save();
    }
}

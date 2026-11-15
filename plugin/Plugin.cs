using MediaBrowser.Common.Api;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Common.Plugins;
using MediaBrowser.Model.Plugins;
using MediaBrowser.Model.Serialization;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace Jellyfin.Plugin.SubtitleTranslator;

public sealed class Configuration : BasePluginConfiguration { }

public sealed class Plugin : BasePlugin<Configuration>, IHasWebPages
{
    public Plugin(IApplicationPaths paths, IXmlSerializer serializer) : base(paths, serializer) { }
    public override string Name => "Subtitle Translator";
    public override string Description => "Translate subtitle tracks with OpenAI, Gemini, Anthropic, Meta Muse, or Ollama.";
    public override Guid Id => new("22b284d5-809f-42e2-b6e4-c6432af82c72");
    public IEnumerable<PluginPageInfo> GetPages() => new[] {
        new PluginPageInfo {
            Name = "SubtitleTranslator",
            EmbeddedResourcePath = "Jellyfin.Plugin.SubtitleTranslator.dashboard.html",
            EnableInMainMenu = true,
            MenuSection = "server",
            MenuIcon = "translate"
        }
    };
}

[ApiController]
[Route("SubtitleTranslator")]
[Authorize(Policy = Policies.RequiresElevation)]
public sealed class TranslatorController : ControllerBase
{
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(25) };

    private async Task<IActionResult> Forward(string path, HttpMethod method, JsonElement? data = null)
    {
        var token = Environment.GetEnvironmentVariable("SUBTITLE_TRANSLATOR_TOKEN");
        if (string.IsNullOrEmpty(token))
            return StatusCode(503, new { error = "Translation service token is not configured. Restart Jellyfin after installation." });
        using var request = new HttpRequestMessage(method, "http://subtitle-translator:8788/" + path);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        if (data.HasValue)
            request.Content = new StringContent(data.Value.GetRawText(), Encoding.UTF8, "application/json");
        try
        {
            using var response = await Client.SendAsync(request, HttpContext.RequestAborted).ConfigureAwait(false);
            var text = await response.Content.ReadAsStringAsync(HttpContext.RequestAborted).ConfigureAwait(false);
            Response.Headers.CacheControl = "no-store";
            return new ContentResult { Content = text, ContentType = "application/json", StatusCode = (int)response.StatusCode };
        }
        catch (HttpRequestException)
        {
            return StatusCode(503, new { error = "Translation service is unavailable. Check the subtitle-translator container." });
        }
        catch (TaskCanceledException)
        {
            return StatusCode(504, new { error = "Translation service timed out. Try again." });
        }
    }

    [HttpGet("Settings")]
    public Task<IActionResult> Settings() => Forward("settings", HttpMethod.Get);
    [HttpPut("Settings")]
    [RequestSizeLimit(16384)]
    public Task<IActionResult> SaveSettings([FromBody] JsonElement body) => Forward("settings", HttpMethod.Put, body);
    [HttpGet("Models")]
    public Task<IActionResult> Models([FromQuery] string provider) => Forward("models?provider=" + Uri.EscapeDataString(provider), HttpMethod.Get);
    [HttpGet("Jobs")]
    public Task<IActionResult> Jobs() => Forward("jobs", HttpMethod.Get);
    [HttpPost("Jobs")]
    [RequestSizeLimit(4194304)]
    public Task<IActionResult> Create([FromBody] JsonElement body) => Forward("jobs", HttpMethod.Post, body);
    [HttpPost("Jobs/{id:guid}/Cancel")]
    public Task<IActionResult> Cancel(Guid id) => Forward($"jobs/{id}/cancel", HttpMethod.Post);
    [HttpPost("Jobs/{id:guid}/Retry")]
    public Task<IActionResult> Retry(Guid id) => Forward($"jobs/{id}/retry", HttpMethod.Post);
    [HttpPost("Jobs/{id:guid}/Saved")]
    public Task<IActionResult> Saved(Guid id) => Forward($"jobs/{id}/saved", HttpMethod.Post);
    [HttpGet("Jobs/{id:guid}/Subtitle")]
    public Task<IActionResult> Subtitle(Guid id) => Forward($"jobs/{id}/subtitle", HttpMethod.Get);
}

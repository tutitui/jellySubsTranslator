using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using MediaBrowser.Common.Configuration;
using MediaBrowser.Controller;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.Plugins;
using MediaBrowser.Controller.Providers;
using MediaBrowser.Controller.Subtitles;
using MediaBrowser.Model.IO;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.SubtitleTranslator;

public sealed class ServiceRegistrator : IPluginServiceRegistrator
{
    public void RegisterServices(IServiceCollection services, IServerApplicationHost applicationHost) => services.AddHostedService<SubtitlePublisher>();
}

/// <summary>Save explicitly requested folder translations after completion, even if the browser closes.</summary>
public sealed class SubtitlePublisher(ILibraryManager library, ISubtitleManager subtitles, IProviderManager providers,
    IFileSystem fileSystem, IApplicationPaths paths, ILogger<SubtitlePublisher> logger) : BackgroundService
{
    private static readonly HttpClient Client = new() { Timeout = TimeSpan.FromSeconds(20) };
    private async Task<JsonDocument> Worker(string route, HttpMethod method, CancellationToken token)
    {
        using var request = new HttpRequestMessage(method, "http://subtitle-translator:8788/" + route);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", Environment.GetEnvironmentVariable("SUBTITLE_TRANSLATOR_TOKEN"));
        if (method == HttpMethod.Post) request.Content = new StringContent("{}", Encoding.UTF8, "application/json");
        using var response = await Client.SendAsync(request, token);
        response.EnsureSuccessStatusCode();
        return JsonDocument.Parse(await response.Content.ReadAsStringAsync(token));
    }
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("SUBTITLE_TRANSLATOR_TOKEN"))) return;
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                using var jobs = await Worker("jobs", HttpMethod.Get, stoppingToken);
                foreach (var job in jobs.RootElement.EnumerateArray())
                {
                    if (!job.TryGetProperty("auto_save", out var autosave) || autosave.GetInt32() != 1 || job.GetProperty("state").GetString() != "completed"
                        || job.GetProperty("saved").GetBoolean() || !string.IsNullOrEmpty(job.GetProperty("error").GetString())) continue;
                    var id = job.GetProperty("id").GetString()!;
                    try
                    {
                        var receipt = Path.Combine(paths.DataPath, "subtitle-translator-receipts", Guid.Parse(id).ToString("N") + ".saved");
                        if (!System.IO.File.Exists(receipt))
                        {
                            using var result = await Worker($"jobs/{id}/subtitle", HttpMethod.Get, stoppingToken);
                            var item = library.GetItemById<Video>(Guid.Parse(result.RootElement.GetProperty("item_id").GetString()!));
                            if (item is null) throw new IOException("Video no longer exists.");
                            await using var stream = new MemoryStream(Encoding.UTF8.GetBytes(result.RootElement.GetProperty("srt").GetString()!));
                            await subtitles.UploadSubtitle(item, new SubtitleResponse { Format = "srt", Language = result.RootElement.GetProperty("target").GetString(), Stream = stream });
                            providers.QueueRefresh(item.Id, new MetadataRefreshOptions(new DirectoryService(fileSystem)), RefreshPriority.High);
                            Directory.CreateDirectory(Path.GetDirectoryName(receipt)!);
                            await System.IO.File.WriteAllTextAsync(receipt, DateTimeOffset.UtcNow.ToString("O"), stoppingToken);
                        }
                        using var saved = await Worker($"jobs/{id}/saved", HttpMethod.Post, stoppingToken);
                    }
                    catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { return; }
                    catch (Exception)
                    {
                        logger.LogWarning("Subtitle Translator could not save job {JobId}; see translation history.", id);
                        using var error = await Worker($"jobs/{id}/save-error", HttpMethod.Post, stoppingToken);
                    }
                }
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { return; }
            catch (Exception) { /* Worker startup/recreation is temporary; retry without logging credentials or payloads. */ }
            try { await Task.Delay(TimeSpan.FromSeconds(5), stoppingToken); }
            catch (OperationCanceledException) { return; }
        }
    }
}

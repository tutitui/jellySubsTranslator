using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using MediaBrowser.Common.Api;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;

namespace Jellyfin.Plugin.SubtitleTranslator;

internal record SubtitleFile(string Id, string Name, string Path, string Language, string LanguageName, bool CanTranslate);

[ApiController]
[Route("SubtitleTranslator/Items")]
[Authorize(Policy = Policies.RequiresElevation)]
public sealed class SubtitleFilesController(ILibraryManager library) : ControllerBase
{
    private const int MaxBytes = 2 * 1024 * 1024;
    private static readonly HashSet<string> Extensions = new(StringComparer.OrdinalIgnoreCase) { ".srt", ".ass", ".ssa", ".vtt", ".ttml", ".sub", ".idx", ".sup", ".smi" };
    private static readonly HashSet<string> TextExtensions = new(StringComparer.OrdinalIgnoreCase) { ".srt", ".ass", ".ssa", ".vtt", ".ttml" };
    internal static readonly Dictionary<string, string> Languages = new() {
        ["pt-BR"] = "Brazilian Portuguese", ["pt-PT"] = "European Portuguese", ["en"] = "English", ["es"] = "Spanish",
        ["fr"] = "French", ["de"] = "German", ["it"] = "Italian", ["ja"] = "Japanese", ["ko"] = "Korean",
        ["zh"] = "Chinese", ["ar"] = "Arabic", ["ru"] = "Russian", ["hi"] = "Hindi"
    };
    private static readonly Dictionary<string, (string Code, string Name)> LanguageAliases = BuildLanguages();
    private static Dictionary<string, (string, string)> BuildLanguages()
    {
        var result = new Dictionary<string, (string, string)>(StringComparer.OrdinalIgnoreCase);
        foreach (var c in CultureInfo.GetCultures(CultureTypes.NeutralCultures))
        {
            if (string.IsNullOrEmpty(c.Name)) continue;
            foreach (var alias in new[] { c.TwoLetterISOLanguageName, c.ThreeLetterISOLanguageName, c.EnglishName })
                result.TryAdd(alias, (c.Name, c.EnglishName));
        }
        foreach (var alias in new[] { "pt-BR", "ptbr", "pob", "brazilian", "brazil" }) result[alias] = ("pt-BR", "Brazilian Portuguese");
        foreach (var alias in new[] { "pt-PT", "ptpt" }) result[alias] = ("pt-PT", "European Portuguese");
        result["fre"] = ("fr", "French"); result["ger"] = ("de", "German"); result["dut"] = ("nl", "Dutch"); result["chi"] = ("zh", "Chinese");
        return result;
    }
    private Video? Video(Guid id) => library.GetItemById<Video>(id);
    private static (string Code, string Name) DetectLanguage(string path, Video item)
    {
        var stem = Path.GetFileNameWithoutExtension(path).Replace("pt_BR", "pt-BR", StringComparison.OrdinalIgnoreCase).Replace("pt_PT", "pt-PT", StringComparison.OrdinalIgnoreCase);
        var tokens = Regex.Split(stem, @"[._\s]+");
        foreach (var token in tokens.Reverse())
        {
            if (LanguageAliases.TryGetValue(token, out var language)) return language;
            if (token.Equals("forced", StringComparison.OrdinalIgnoreCase) || token.Equals("sdh", StringComparison.OrdinalIgnoreCase) || token.Equals("cc", StringComparison.OrdinalIgnoreCase) || int.TryParse(token, out _)) continue;
            break;
        }
        var stream = item.GetMediaStreams().FirstOrDefault(s => s.IsExternal && string.Equals(s.Path, path, StringComparison.Ordinal));
        if (stream?.Language is { Length: > 0 } code && LanguageAliases.TryGetValue(code, out var known)) return known;
        return ("auto-detect", "Unknown language");
    }
    private static IEnumerable<SubtitleFile> Discover(Video item)
    {
        var folder = Path.GetDirectoryName(item.Path);
        if (string.IsNullOrEmpty(folder) || !Directory.Exists(folder)) return [];
        return Directory.EnumerateFiles(folder, "*", SearchOption.TopDirectoryOnly)
            .Where(path => Extensions.Contains(Path.GetExtension(path)))
            .Where(path => (System.IO.File.GetAttributes(path) & FileAttributes.ReparsePoint) == 0)
            .OrderBy(Path.GetFileName, StringComparer.OrdinalIgnoreCase)
            .Take(500)
            .Select(path => {
                var language = DetectLanguage(path, item);
                return new SubtitleFile(Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(path)))),
                    Path.GetFileName(path), path, language.Code, language.Name,
                    TextExtensions.Contains(Path.GetExtension(path)) && new FileInfo(path).Length <= MaxBytes);
            }).ToArray();
    }
    [HttpGet("{id:guid}/Files")]
    public IActionResult Files(Guid id)
    {
        var item = Video(id);
        if (item is null) return NotFound(new { error = "Video not found." });
        try { return Ok(new { itemId = item.Id, files = Discover(item).Select(f => new { id = f.Id, name = f.Name, language = f.Language, languageName = f.LanguageName, canTranslate = f.CanTranslate }), languages = Languages }); }
        catch (IOException) { return BadRequest(new { error = "Cannot read the movie folder." }); }
        catch (UnauthorizedAccessException) { return BadRequest(new { error = "Jellyfin cannot read the movie folder." }); }
    }
    [HttpGet("{id:guid}/Files/{fileId}/Srt")]
    public async Task<IActionResult> Srt(Guid id, string fileId)
    {
        var item = Video(id);
        if (item is null) return NotFound();
        var file = Discover(item).FirstOrDefault(f => f.Id == fileId);
        if (file is null) return NotFound(new { error = "Subtitle file no longer exists." });
        if (!file.CanTranslate) return BadRequest(new { error = "This subtitle needs conversion or OCR before translation." });
        try
        {
            string text;
            if (Path.GetExtension(file.Path).Equals(".srt", StringComparison.OrdinalIgnoreCase))
                text = await System.IO.File.ReadAllTextAsync(file.Path, HttpContext.RequestAborted);
            else
            {
                var binary = new[] { "/usr/lib/jellyfin-ffmpeg/ffmpeg", "/usr/bin/ffmpeg" }.FirstOrDefault(System.IO.File.Exists);
                if (binary is null) return BadRequest(new { error = "FFmpeg is unavailable for subtitle conversion." });
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(HttpContext.RequestAborted);
                timeout.CancelAfter(TimeSpan.FromSeconds(30));
                var start = new ProcessStartInfo(binary) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
                foreach (var arg in new[] { "-nostdin", "-hide_banner", "-loglevel", "error", "-i", file.Path, "-map", "0:s:0", "-f", "srt", "pipe:1" }) start.ArgumentList.Add(arg);
                using var process = Process.Start(start)!;
                try {
                    var output = process.StandardOutput.ReadToEndAsync(timeout.Token);
                    var errors = process.StandardError.ReadToEndAsync(timeout.Token);
                    await process.WaitForExitAsync(timeout.Token);
                    text = await output; await errors;
                    if (process.ExitCode != 0) return BadRequest(new { error = "Could not convert this subtitle to SRT." });
                } catch { if (!process.HasExited) process.Kill(true); throw; }
            }
            if (Encoding.UTF8.GetByteCount(text) > MaxBytes) return BadRequest(new { error = "Subtitle is too large." });
            return Ok(new { srt = text, fileId = file.Id, language = file.Language });
        }
        catch (OperationCanceledException) { return BadRequest(new { error = "Subtitle conversion timed out." }); }
        catch (IOException) { return BadRequest(new { error = "Subtitle file could not be read." }); }
        catch (UnauthorizedAccessException) { return BadRequest(new { error = "Jellyfin cannot read this subtitle file." }); }
    }
}

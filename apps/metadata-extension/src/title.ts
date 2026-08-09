import type { LanguageCode, Preferences, TitleSuffix } from "./types";

const SUFFIXES: Record<LanguageCode, Record<Exclude<TitleSuffix, "none">, string>> = {
  en: { transparent: " isolated on Transparent Background", white: " isolated on White Background", png_transparent: " isolated PNG with Transparent Background" },
  fr: { transparent: " isolé sur Fond Transparent", white: " isolé sur Fond Blanc", png_transparent: " PNG isolé avec Fond Transparent" },
  de: { transparent: " isoliert auf Transparentem Hintergrund", white: " isoliert auf Weißem Hintergrund", png_transparent: " isoliertes PNG mit Transparentem Hintergrund" },
  es: { transparent: " aislado en Fondo Transparente", white: " aislado en Fondo Blanco", png_transparent: " PNG aislado con Fondo Transparente" },
  it: { transparent: " isolato su Sfondo Trasparente", white: " isolato su Sfondo Bianco", png_transparent: " PNG isolato con Sfondo Trasparente" },
  pt: { transparent: " isolado em Fundo Transparente", white: " isolado em Fundo Branco", png_transparent: " PNG isolado com Fundo Transparente" },
  ja: { transparent: " 透明な背景に分離", white: " 白い背景に分離", png_transparent: " 透明な背景の分離PNG" },
  pl: { transparent: " izolowane na Przezroczystym Tle", white: " izolowane na Białym Tle", png_transparent: " izolowany PNG z Przezroczystym Tłem" },
  ko: { transparent: " 투명 배경에 분리", white: " 흰색 배경에 분리", png_transparent: " 투명 배경의 분리 PNG" },
};

const BACKGROUND_PHRASES = /\s+(?:(?:on|with|against|in)\s+(?:a\s+|an\s+)?(?:white|transparent|black|blue|gray|grey|colou?red|neutral|plain|simple|clean)\s+(?:background|backdrop|surface|bg)|isolated(?:\s+on\s+.*)?|png\s+with\s+transparent\s+background)$/gi;

export function applyTitleRules(title: string, preferences: Preferences): string {
  let result = title.trim();
  if (preferences.titleSuffix !== "none") {
    result = result.replace(BACKGROUND_PHRASES, "").trim();
    result += SUFFIXES[preferences.language][preferences.titleSuffix];
  }
  if (preferences.titlePrefix.trim()) result = `${preferences.titlePrefix.trim()} ${result}`;
  if (preferences.customTitleSuffix.trim()) result = `${result} ${preferences.customTitleSuffix.trim()}`;
  if (result.length > 195) {
    let shortened = result.slice(0, 195);
    const comma = shortened.lastIndexOf(",");
    const space = shortened.lastIndexOf(" ");
    if (comma > 145) shortened = shortened.slice(0, comma);
    else if (space > 165) shortened = shortened.slice(0, space);
    result = shortened.trim();
  }
  return result;
}

export function expandCustomPrompt(text: string, preferences: Preferences, assetType: string): string {
  const languageNames: Record<LanguageCode, string> = {
    en: "English", fr: "French", de: "German", es: "Spanish", it: "Italian", pt: "Portuguese", ja: "Japanese", pl: "Polish", ko: "Korean",
  };
  const categoryInstruction = "Return categoryId as one integer from the sub2gen Adobe Stock taxonomy (1-21).";
  return text
    .replaceAll("{language}", languageNames[preferences.language])
    .replaceAll("{fileType}", assetType || "photo")
    .replaceAll("{category}", preferences.includeCategory ? categoryInstruction : "");
}

export type ProcessingMode = "upload" | "portfolio";
export type LanguageCode = "en" | "fr" | "de" | "es" | "it" | "pt" | "ja" | "pl" | "ko";
export type TitleSuffix = "none" | "transparent" | "white" | "png_transparent";
export type TitleStyle = "seo-optimized" | "descriptive" | "creative" | "commercial";
export type KeywordStyle = "single-word" | "double-word" | "mixed";
export type RunPhase = "idle" | "starting" | "running" | "pausing" | "paused" | "completed" | "error";
export type ActivityPhase = "generating" | "applying" | "saving" | "success" | "error";

export interface RunActivity {
  id: string;
  assetNumber: number;
  page: number;
  phase: ActivityPhase;
  message: string;
  updatedAt: number;
}

export interface Connection {
  baseUrl: string;
  apiKey: string;
  keyLabel: string;
  validatedAt: number;
}

export interface Preferences {
  mode: ProcessingMode;
  includeCategory: boolean;
  language: LanguageCode;
  titleSuffix: TitleSuffix;
  titlePrefix: string;
  customTitleSuffix: string;
  titleMin: number;
  titleMax: number;
  keywordMin: number;
  keywordMax: number;
  descriptionMin: number;
  descriptionMax: number;
  platforms: string[];
  customPlatforms: string;
  includeReleases: boolean;
  titleStyle: TitleStyle;
  keywordStyle: KeywordStyle;
  transparentBackground: boolean;
  markGenerativeAi: boolean;
  confirmFictionalPeopleProperty: boolean;
  customPromptEnabled: boolean;
  customPrompt: string;
}

export interface RuntimeState {
  processing: boolean;
  stopped: boolean;
  phase: RunPhase;
  ownerTabId: number | null;
  ownerWindowId: number | null;
  processed: number;
  successes: number;
  currentPage: number;
  currentIndex: number;
  pageTotal: number;
  targetTotal: number | null;
  activities: RunActivity[];
  message: string;
}

export interface MetadataOption {
  title?: unknown;
  keywords?: unknown;
  categoryId?: unknown;
}

export interface Sub2GenMetadataResponse {
  optionA?: MetadataOption;
  optionB?: MetadataOption;
}

export interface GeneratedMetadata {
  title: string;
  keywords: string;
  category: string;
}

export interface SessionResponse {
  active: boolean;
  service: "sub2gen-metadata";
  keyLabel: string;
  capabilities: string[];
}

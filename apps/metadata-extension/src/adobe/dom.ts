import type { GeneratedMetadata, Preferences, ProcessingMode } from "../types";

export const UPLOAD_IMAGES = ".upload-tile__thumbnail.upload-tile__thumbnail--portrait, .upload-tile__thumbnail.upload-tile__thumbnail--landscape";
export const PORTFOLIO_IMAGES = ".content-thumbnail__img";
export const TITLE_INPUTS = 'textarea[data-t="asset-title-content-tagger"], textarea[name="title"], textarea[id="content-title-ui-textarea"], textarea[aria-label="Content title"], textarea[aria-label="Inhaltstitel"], textarea[aria-label="Titel"]';
export const KEYWORD_INPUTS = 'textarea[id="content-keywords-ui-textarea"], textarea[data-t="content-keywords-ui-textarea"], textarea[name="keywordsUITextArea"], textarea[aria-label*="Keyword"], textarea[aria-label*="Stichw"]';

export const delay = (milliseconds: number) => new Promise((resolve) => setTimeout(resolve, milliseconds));

export async function waitFor<T extends Element>(selector: string, timeout = 8_000): Promise<T> {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const element = document.querySelector<T>(selector);
    if (element) return element;
    await delay(100);
  }
  throw new Error(`Timed out waiting for Adobe field: ${selector}`);
}

export function assetImages(mode: ProcessingMode): HTMLImageElement[] {
  return Array.from(document.querySelectorAll<HTMLImageElement>(mode === "upload" ? UPLOAD_IMAGES : PORTFOLIO_IMAGES));
}

export function setNativeValue(element: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string): void {
  const prototype = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : element instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  if (setter) setter.call(element, value);
  else element.value = value;
  element.dispatchEvent(new Event("input", { bubbles: true }));
  element.dispatchEvent(new Event("change", { bubbles: true }));
}

function enter(element: HTMLElement): void {
  element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
}

function normalizedText(value: string | null | undefined): string {
  return (value || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function checkboxLabel(input: HTMLInputElement): string {
  const explicit = input.id
    ? Array.from(document.querySelectorAll<HTMLLabelElement>("label[for]")).find((label) => label.htmlFor === input.id) ?? null
    : null;
  const container = input.closest<HTMLElement>('label, [role="checkbox"], [class*="Checkbox"], [class*="checkbox"]');
  const parentText = input.parentElement && input.parentElement !== document.body
    && normalizedText(input.parentElement.textContent).length <= 220
    ? input.parentElement.textContent
    : "";
  return normalizedText([
    input.getAttribute("aria-label"),
    explicit?.textContent,
    container?.textContent,
    parentText,
  ].filter(Boolean).join(" "));
}

function findCheckbox(pattern: RegExp): HTMLInputElement | null {
  return Array.from(document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'))
    .find((input) => pattern.test(checkboxLabel(input))) ?? null;
}

async function waitForCheckbox(pattern: RegExp, timeout = 5_000): Promise<HTMLInputElement | null> {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    const input = findCheckbox(pattern);
    if (input) return input;
    await delay(100);
  }
  return null;
}

async function setCheckboxState(input: HTMLInputElement, checked: boolean, name: string): Promise<void> {
  if (input.checked !== checked) input.click();
  await delay(100);
  if (input.checked !== checked) throw new Error(`Adobe did not apply the ${name} declaration.`);
}

async function chooseNoRecognizablePeopleOrProperty(): Promise<boolean> {
  const candidates = Array.from(document.querySelectorAll<HTMLElement>("fieldset, section, div"))
    .filter((element) => normalizedText(element.textContent).includes("recognizable people or property"))
    .sort((a, b) => (a.textContent?.length || 0) - (b.textContent?.length || 0));
  const container = candidates[0];
  if (!container) return false;
  const noInput = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="radio"], input[type="checkbox"]'))
    .find((input) => normalizedText(input.value) === "no" || normalizedText(input.getAttribute("aria-label")) === "no");
  if (noInput) {
    if (!noInput.checked) noInput.click();
    await delay(100);
    return noInput.checked;
  }
  const noButton = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
    .find((button) => normalizedText(button.textContent) === "no" || normalizedText(button.getAttribute("aria-label")) === "no");
  if (!noButton) return false;
  noButton.click();
  await delay(100);
  return true;
}

export async function applyAdobeAiDeclarations(preferences: Preferences): Promise<void> {
  const ai = await waitForCheckbox(/created using generative ai tools?/i, 5_000);
  if (!ai) throw new Error('Adobe checkbox "Created using generative AI tools" was not found.');
  await setCheckboxState(ai, preferences.markGenerativeAi, "generative AI");
  if (!preferences.markGenerativeAi) return;

  const started = Date.now();
  while (Date.now() - started < 2_500) {
    const fictional = findCheckbox(/people and property are fictional/i);
    if (fictional) {
      await setCheckboxState(fictional, preferences.confirmFictionalPeopleProperty, "fictional people and property");
      return;
    }
    if (preferences.confirmFictionalPeopleProperty && await chooseNoRecognizablePeopleOrProperty()) return;
    await delay(100);
  }
  if (preferences.confirmFictionalPeopleProperty) throw new Error("Adobe fictional people/property control was not found.");
}

export function detectAssetType(mode: ProcessingMode): string {
  if (mode === "portfolio") {
    return document.querySelector<HTMLElement>('[data-t="portfolio-detail-panel-format"]')?.textContent?.trim().toLowerCase() || "photo";
  }
  const select = document.querySelector<HTMLSelectElement>('select[name="contentType"]');
  if (select?.selectedOptions[0]?.textContent) return select.selectedOptions[0].textContent.trim().toLowerCase();
  const label = document.querySelector<HTMLElement>(".cm4dRG_spectrum-Dropdown-label")?.textContent?.trim().toLowerCase();
  return label?.replace(/s$/, "") || "photo";
}

async function chooseCategory(category: string): Promise<void> {
  if (!category) return;
  let option = document.querySelector<HTMLElement>(`[role="option"][data-key="${category}"]`);
  if (!option) {
    document.querySelector<HTMLElement>('button[data-t="content-tagger-category-select"]')?.click();
    await delay(150);
    option = document.querySelector<HTMLElement>(`[role="option"][data-key="${category}"]`);
  }
  if (option) {
    option.click();
    return;
  }
  const select = document.querySelector<HTMLSelectElement>('select[name="category"], select[aria-label="Category"], select[aria-label="Kategorie"], select.input--full');
  if (select) setNativeValue(select, category);
}

function saveControlText(control: HTMLButtonElement | HTMLInputElement): string {
  return normalizedText(control instanceof HTMLInputElement ? control.value || control.getAttribute("aria-label") : control.textContent || control.getAttribute("aria-label"));
}

function findSaveWorkControl(): HTMLButtonElement | HTMLInputElement | null {
  const controls = Array.from(document.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button, input[type="submit"], input[type="button"]'));
  return controls.find((control) => saveControlText(control) === "save work")
    ?? document.querySelector<HTMLButtonElement>('button[type="submit"][data-test="save-metadata"], .button--action[type="submit"], button.button--action');
}

function hasSavedConfirmation(): boolean {
  return Array.from(document.querySelectorAll<HTMLElement>('[role="status"], [role="alert"], [class*="toast"], [class*="notification"]'))
    .some((element) => /(?:work|changes|metadata) saved|saved successfully/i.test(normalizedText(element.textContent)));
}

export async function saveAdobeForm(): Promise<void> {
  const started = Date.now();
  let button = findSaveWorkControl();
  while ((!button || button.disabled || button.getAttribute("aria-disabled") === "true") && Date.now() - started < 8_000) {
    await delay(100);
    button = findSaveWorkControl();
  }
  if (!button) throw new Error('Adobe "Save work" button was not found.');
  if (button.disabled || button.getAttribute("aria-disabled") === "true") throw new Error('Adobe "Save work" button did not become available.');

  button.click();
  const clickedAt = Date.now();
  let sawSavingState = false;
  while (Date.now() - clickedAt < 10_000) {
    if (hasSavedConfirmation()) return;
    const current = findSaveWorkControl() ?? button;
    const busy = current.disabled
      || current.getAttribute("aria-disabled") === "true"
      || current.getAttribute("aria-busy") === "true"
      || /saving|saved/.test(saveControlText(current));
    if (busy) sawSavingState = true;
    if (sawSavingState && Date.now() - clickedAt >= 500) {
      if (current.disabled || current.getAttribute("aria-disabled") === "true") return;
      if (current.getAttribute("aria-busy") !== "true" && !/saving/.test(saveControlText(current))) return;
    }
    await delay(100);
  }
  throw new Error('Adobe did not confirm "Save work" completion.');
}

export async function openAsset(image: HTMLImageElement, mode: ProcessingMode): Promise<void> {
  image.scrollIntoView({ behavior: "auto", block: "center" });
  image.click();
  if (mode === "upload") await waitFor(`${TITLE_INPUTS}, ${KEYWORD_INPUTS}`);
  else await waitFor(".editable__content, .keywords-section, .editable__pencil, .content-detail", 10_000);
}

export async function applyUploadMetadata(metadata: GeneratedMetadata, preferences: Preferences, onSaving?: () => void | Promise<void>): Promise<void> {
  const title = await waitFor<HTMLTextAreaElement>(TITLE_INPUTS);
  const keywords = await waitFor<HTMLTextAreaElement>(KEYWORD_INPUTS);
  setNativeValue(title, metadata.title);
  title.blur();
  setNativeValue(keywords, metadata.keywords);
  enter(keywords);
  await delay(250);
  await chooseCategory(metadata.category);
  await applyAdobeAiDeclarations(preferences);
  await onSaving?.();
  await saveAdobeForm();
}

export async function applyPortfolioMetadata(metadata: GeneratedMetadata, onSaving?: () => void | Promise<void>): Promise<void> {
  const titlePencil = await waitFor<HTMLElement>(".editable__pencil");
  titlePencil.click();
  const titleInput = await waitFor<HTMLInputElement>(".input--full");
  setNativeValue(titleInput, metadata.title);
  document.querySelector<HTMLElement>(".button__text.text-up")?.click();
  await delay(250);

  const keywordPencil = await waitFor<HTMLElement>(".button.button--floating.editable__pencil.margin-left-small");
  keywordPencil.click();
  await delay(250);
  const keywords = metadata.keywords.split(",").map((item) => item.trim()).filter(Boolean);
  let inputs = Array.from(document.querySelectorAll<HTMLInputElement>('[data-t="content-keyword"]'));
  if (!inputs.length) throw new Error("Adobe keyword editor did not expose keyword inputs.");
  for (let index = 0; index < Math.max(inputs.length, keywords.length); index += 1) {
    inputs = Array.from(document.querySelectorAll<HTMLInputElement>('[data-t="content-keyword"]'));
    const input = inputs[index];
    if (!input) break;
    setNativeValue(input, keywords[index] || "");
    if (keywords[index]) enter(input);
    await delay(60);
  }
  document.querySelector<HTMLElement>(".button.button--dialog")?.click();
  await delay(250);
  await chooseCategory(metadata.category);
  await onSaving?.();
  await saveAdobeForm();
}

export function nextPageButton(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".pagination__item--next:not(.pagination__item--disabled)")
    ?? Array.from(document.querySelectorAll<HTMLElement>("button, a")).find((element) => {
      const text = element.textContent?.trim().toLowerCase();
      return text === "next" || element.getAttribute("aria-label")?.toLowerCase().includes("next");
    }) ?? null;
}

export function addProcessingOverlay(image: HTMLImageElement, index: number, total: number): () => void {
  const container = image.closest<HTMLElement>(".upload-tile, .content-thumbnail, [class*='thumbnail']") ?? image.parentElement;
  if (!container) return () => undefined;
  container.style.position = "relative";
  const overlay = document.createElement("div");
  overlay.className = "sub2gen-metadata-processing";
  overlay.textContent = `Processing ${index + 1} of ${total}`;
  Object.assign(overlay.style, {
    position: "absolute", inset: "5px", zIndex: "9999", display: "grid", placeContent: "center",
    borderRadius: "8px", background: "rgba(8,16,35,.78)", color: "white", font: "600 12px system-ui", pointerEvents: "none",
  });
  container.appendChild(overlay);
  return () => overlay.remove();
}

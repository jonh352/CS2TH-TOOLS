"""CS2TH desktop design system.

The application keeps one QSS source of truth: page code only assigns semantic
object names and dynamic state properties. This avoids duplicated per-page
styles and keeps theme changes inexpensive and visually consistent.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from config import ASSETS_DIR


PALETTES = {
    "dark": {
        "bg": "#060A11",
        "surface": "#0B121E",
        "surface_alt": "#0E1826",
        "card": "#101B2B",
        "card_hover": "#142338",
        "input": "#08111D",
        "border": "#203149",
        "border_strong": "#31506F",
        "text": "#EAF4FF",
        "muted": "#8AA0B8",
        "dim": "#52677D",
        "cyan": "#19C7FF",
        "cyan_hover": "#5AD9FF",
        "cyan_dim": "#0C3042",
        "blue": "#3B82F6",
        "lime": "#7CE38B",
        "lime_dim": "#103625",
        "rose": "#FF5C78",
        "rose_dim": "#3A1520",
        "amber": "#F5B942",
        "amber_dim": "#3A2B11",
        "primary_text": "#03131B",
        "overlay": "rgba(2, 6, 12, 196)",
        "check_icon": "check_dark.svg",
    },
    "light": {
        "bg": "#F3F7FB",
        "surface": "#FFFFFF",
        "surface_alt": "#F8FBFE",
        "card": "#FFFFFF",
        "card_hover": "#F1F7FC",
        "input": "#F7FAFD",
        "border": "#D5E0EA",
        "border_strong": "#AFC4D6",
        "text": "#102235",
        "muted": "#637A91",
        "dim": "#91A4B6",
        "cyan": "#078DB5",
        "cyan_hover": "#047A9F",
        "cyan_dim": "#DDF4FA",
        "blue": "#2563EB",
        "lime": "#3F9E55",
        "lime_dim": "#E3F5E7",
        "rose": "#DC3F5D",
        "rose_dim": "#FCE7EC",
        "amber": "#B97808",
        "amber_dim": "#FFF2D6",
        "primary_text": "#F7FDFF",
        "overlay": "rgba(15, 31, 47, 132)",
        "check_icon": "check.svg",
    },
}


def _asset_url(name: str) -> str:
    return str(ASSETS_DIR / name).replace("\\", "/")


def build_stylesheet(name: str) -> str:
    # Theme-dependent colors use QPalette roles.  The structural QSS is
    # installed only once; subsequent dark/light switches merely replace the
    # palette instead of forcing every widget to reparse this stylesheet.
    c = {
        "bg": "palette(window)",
        "surface": "palette(button)",
        "surface_alt": "palette(alternate-base)",
        "card": "palette(button)",
        "card_hover": "palette(alternate-base)",
        "input": "palette(base)",
        "border": "palette(light)",
        "border_strong": "palette(midlight)",
        "text": "palette(text)",
        "muted": "palette(dark)",
        "dim": "palette(mid)",
        "cyan": "palette(highlight)",
        "cyan_hover": "palette(link)",
        "cyan_dim": "palette(shadow)",
        "blue": "#3B82F6",
        "lime": "palette(tool-tip-text)",
        "lime_dim": "palette(alternate-base)",
        "rose": "palette(bright-text)",
        "rose_dim": "palette(alternate-base)",
        "amber": "palette(link-visited)",
        "amber_dim": "palette(alternate-base)",
        "primary_text": "palette(highlighted-text)",
        "overlay": "rgba(2, 6, 12, 196)",
        "check_icon": _asset_url("check_dark.svg"),
    }
    return """
/* ---------- Foundation ---------- */
* {{
  font-family: "Microsoft YaHei UI", "Segoe UI";
  font-size: 13px;
  color: {text};
  selection-background-color: {cyan};
  selection-color: {primary_text};
}}
QMainWindow, QWidget#appRoot {{
  background: {bg};
}}
QWidget#contentArea, QWidget#alchemyPage, QWidget#alchemySimulationPage,
QWidget#recipeManagePage, QWidget#collectionPresetPage {{
  background: {bg};
}}
QLabel {{
  background: transparent;
}}
QLabel#muted, QLabel#statusLabel, QLabel#brandSub,
QLabel#alchemyStep1Hint, QLabel#alchemyStep2NormLabel,
QLabel#recipePageTitleCount, QLabel#recipeFolderHeading,
QLabel#alchemySimulationPriceBasisHint, QLabel#moveRecipeFolderHint {{
  color: {muted};
}}

/* ---------- App chrome ---------- */
QFrame#topBar {{
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {surface}, stop:0.58 {surface}, stop:1 {surface_alt}
  );
  border: 0;
  border-bottom: 1px solid {border};
}}
QLabel#brandName {{
  color: {text};
  font-size: 21px;
  font-weight: 800;
}}
QLabel#brandSub {{
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1px;
}}
QFrame#brandDivider {{
  background: {border};
  border: 0;
}}
  QFrame#navigationHistoryOverlay {{
    background: transparent;
    border: 0;
  }}
  QPushButton#navigationHistoryButton {{
    min-width: 26px;
    max-width: 26px;
    min-height: 18px;
    max-height: 18px;
  padding: 0;
  color: {text};
  background: transparent;
  border: 1px solid transparent;
    border-radius: 6px;
    font-size: 18px;
    font-weight: 900;
}}
QPushButton#navigationHistoryButton:hover {{
  color: {cyan};
  background: {cyan_dim};
  border-color: {border_strong};
}}
QPushButton#navigationHistoryButton:pressed {{
  color: {cyan_hover};
  background: {card_hover};
  border-color: {cyan};
}}
QPushButton#navigationHistoryButton:disabled {{
  color: {dim};
  background: transparent;
  border-color: transparent;
}}
QPushButton#navButton {{
  min-height: 38px;
  padding: 0 13px;
  color: {muted};
  background: transparent;
  border: 1px solid transparent;
  border-radius: 9px;
  font-weight: 650;
}}
QPushButton#navButton:hover {{
  color: {text};
  background: {card_hover};
  border-color: {border};
}}
QPushButton#navButton[active="true"] {{
  color: {cyan};
  background: {cyan_dim};
  border-color: {border_strong};
}}
QPushButton#accountButton, QPushButton#themeButton, QPushButton#settingsButton {{
  min-height: 36px;
  background: {input};
  border-color: {border};
}}
QPushButton#accountButton:hover, QPushButton#themeButton:hover,
QPushButton#settingsButton:hover {{
  color: {cyan};
  border-color: {cyan};
  background: {cyan_dim};
}}
QPushButton#themeButton, QPushButton#settingsButton {{
  padding: 0;
  font-size: 16px;
  border-radius: 10px;
}}
QWidget#loginBackdrop {{
  background: {overlay};
}}
QDialog#loginDialog {{
  background: transparent;
}}
QFrame#loginCard {{
  background: {surface};
  border: 1px solid {border};
  border-radius: 18px;
}}
QFrame#authBrandAccent {{
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {cyan}, stop:1 {blue}
  );
  border: 0;
  border-radius: 2px;
}}
QLabel#authBrandLogo {{
  background: transparent;
}}
QLabel#authBrandName {{
  color: {text};
  font-size: 17px;
  font-weight: 800;
}}
QLabel#loginFormHeading {{
  color: {text};
  font-size: 15px;
  font-weight: 750;
}}
QLabel#authBrandSub, QLabel#authBrandCaption {{
  color: {muted};
  font-size: 12px;
  font-weight: 650;
}}
QLabel#authBrandProduct {{
  color: {text};
  font-size: 13px;
  font-weight: 750;
}}
QLabel#loginTitle {{
  color: {text};
  font-size: 18px;
  font-weight: 800;
}}
QLabel#loginHint, QLabel#loginFieldLabel {{
  color: {muted};
  font-size: 12px;
}}
QLabel#loginFieldLabel {{
  margin-top: 0;
}}
QLineEdit#loginField {{
  min-height: 40px;
  padding: 0 13px;
  background: {input};
  border: 1px solid {border};
  border-radius: 11px;
  font-size: 13px;
}}
QLineEdit#loginField:focus {{
  border-color: {cyan};
  background: {surface};
}}
QPushButton#loginCloseButton, QPushButton#loginCloseBtn,
QPushButton#settingsModalClose {{
  min-width: 28px;
  max-width: 28px;
  min-height: 28px;
  padding: 0;
  color: {muted};
  background: transparent;
  border: 0;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 400;
}}
QPushButton#loginCloseButton:hover, QPushButton#loginCloseBtn:hover,
QPushButton#settingsModalClose:hover {{
  color: {text};
  background: {card_hover};
}}
QPushButton#loginSubmitButton {{
  min-height: 40px;
  margin-top: 4px;
  color: {primary_text};
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {cyan}, stop:1 {blue}
  );
  border: 1px solid {cyan};
  border-radius: 11px;
  font-size: 14px;
  font-weight: 800;
}}
QPushButton#loginSubmitButton:hover {{
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {cyan_hover}, stop:1 {blue}
  );
  border-color: {cyan_hover};
}}
QLabel#accountDialogUsername {{
  color: {text};
  font-size: 18px;
  font-weight: 800;
  padding: 0;
}}
QFrame#accountDialogEntitlementBox {{
  background: {input};
  border: 1px solid {border};
  border-radius: 10px;
}}
QLabel#accountDialogEntitlementRow {{
  color: {text};
  font-size: 13px;
  font-weight: 600;
}}
QLabel#accountDialogEntitlementEmpty {{
  color: {muted};
  font-size: 13px;
}}
QPushButton#accountDialogLogoutButton {{
  min-height: 38px;
  color: {primary_text};
  background: {rose};
  border: 1px solid {rose};
  border-radius: 10px;
  font-weight: 750;
}}
QPushButton#accountDialogLogoutButton:hover {{
  background: #ff7a90;
  border-color: #ff7a90;
}}
QPushButton#accountDialogStayButton {{
  min-height: 36px;
  color: {muted};
  background: transparent;
  border: 1px solid {border};
  border-radius: 10px;
  font-weight: 650;
}}
QPushButton#accountDialogStayButton:hover {{
  color: {text};
  border-color: {border_strong};
  background: {card_hover};
}}
QLabel#loginFooter, QLabel#loginLink {{
  color: {muted};
  font-size: 12px;
}}
QLabel#loginMessage {{
  padding: 7px 9px;
  margin-bottom: 0;
  color: {lime};
  background: {lime_dim};
  border: 1px solid {border};
  border-radius: 8px;
}}
QLabel#loginMessage[error="true"] {{
  color: {rose};
  background: {rose_dim};
}}
QListWidget#settingsNavigation {{
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 10px;
  outline: none;
  padding: 5px;
}}
QListWidget#settingsNavigation::item {{
  min-height: 38px;
  padding-left: 10px;
  border-radius: 7px;
}}
QListWidget#settingsNavigation::item:selected {{
  color: {cyan};
  background: {cyan_dim};
}}
QRadioButton#settingsRadio {{
  min-height: 58px;
  padding: 8px 12px;
  spacing: 12px;
  background: {input};
  border: 1px solid {border};
  border-radius: 9px;
}}
QRadioButton#settingsRadio:checked {{
  color: {cyan};
  border-color: {cyan};
  background: {cyan_dim};
}}

QFrame#settingsModalPanel {{
  background: {surface};
  border: 1px solid {border_strong};
  border-radius: 16px;
}}
QLabel#settingsModalTitle {{
  color: {text};
  font-size: 18px;
  font-weight: 800;
}}
QLabel#settingsSectionTitle {{
  color: {muted};
  font-size: 12px;
  font-weight: 700;
  margin: 4px 0 8px 0;
}}
QFrame#settingsSectionDivider {{
  background: {border};
  border: none;
  margin: 4px 0 8px 0;
}}
QFrame#settingsRadioCard {{
  background: {input};
  border: 1px solid {border};
  border-radius: 10px;
}}
QFrame#settingsRadioCard[checked="true"] {{
  border-color: {cyan};
  background: {cyan_dim};
}}
QLabel#settingsRadioTitle {{
  color: {text};
  font-size: 14px;
  font-weight: 700;
}}
QLabel#settingsRadioDetail {{
  color: {muted};
  font-size: 11px;
}}
QRadioButton#settingsRadioDot {{
  spacing: 0;
}}
QLabel#settingsUsage {{
  color: {text};
  font-size: 13px;
  margin-bottom: 8px;
}}
QLabel#settingsHint {{
  color: {dim};
  font-size: 11px;
  margin-top: 6px;
}}
QPushButton#settingsLegalLink {{
  color: {cyan};
  background: transparent;
  border: none;
  padding: 0;
  font-size: 13px;
  font-weight: 600;
  text-align: left;
}}
QPushButton#settingsLegalLink:hover {{
  color: {cyan_hover};
}}
QLabel#settingsLegalSep {{
  color: {muted};
  font-size: 13px;
}}
QFrame#aboutCard {{
  background: {card};
  border: 1px solid {border};
  border-radius: 12px;
}}
QFrame#aboutStepBlock {{
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 12px;
}}
QLabel#aboutBody {{
  color: {muted};
  font-size: 14px;
}}
QLabel#aboutFlowTitle {{
  color: {text};
  font-size: 15px;
  font-weight: 700;
  margin-top: 4px;
  margin-bottom: 2px;
}}
QLabel#aboutStepMarker {{
  color: {cyan};
  font-size: 16px;
  font-weight: 750;
}}
QLabel#aboutStepTitle {{
  color: {text};
  font-size: 15px;
  font-weight: 750;
}}
QLabel#aboutStepSummary {{
  color: {muted};
  font-size: 13px;
  padding-left: 30px;
}}
QLabel#aboutGroupTitle {{
  color: {cyan};
  font-size: 13px;
  font-weight: 700;
  margin-top: 4px;
}}
QLabel#aboutBullet,
QLabel#aboutTipItem {{
  color: {text};
  font-size: 13px;
}}
QLabel#aboutStepText {{
  color: {text};
  font-size: 14px;
}}
QLabel#aboutFootNote {{
  color: {dim};
  font-size: 12px;
  margin-top: 2px;
}}
QScrollArea#aboutScroll {{
  background: transparent;
  border: none;
}}
QWidget#aboutScrollInner {{
  background: transparent;
}}
QTextBrowser#legalDocumentBrowser {{
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 12px;
  padding: 4px;
}}

/* ---------- Typography ---------- */
QLabel#pageKicker {{
  color: {cyan};
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 2px;
}}
QLabel#pageTitle {{
  color: {text};
  font-size: 22px;
  font-weight: 800;
}}
QLabel#alchemyPageTitle, QLabel#alchemySimulationPageTitle {{
  color: {text};
  font-size: 22px;
  font-weight: 800;
}}
QLabel#sectionTitle {{
  color: {text};
  font-size: 15px;
  font-weight: 750;
}}
QLabel#alchemyGroupTitle, QLabel#alchemyProductName,
QLabel#alchemySimulationResultName, QLabel#marketCardTitle,
QLabel#alchemySimulationResultsGroupTitle {{
  color: {text};
  font-weight: 700;
}}
QLabel#alchemyGroupCount {{
  color: {muted};
  font-size: 13px;
  font-weight: 650;
}}
QLabel#alchemyProductFieldLabel, QLabel#fetchCustomWearFloatDesc,
QLabel#fetchCustomWearIntervalDesc, QLabel#fetchCustomWearSepLabel {{
  color: {muted};
  font-size: 12px;
}}

/* ---------- Surfaces ---------- */
QFrame#panel, QFrame#metricCard, QFrame#marketCard,
QFrame#alchemyStep1CountCard, QFrame#alchemyStep2NormCard,
QFrame#alchemyGroup, QFrame#recipeFolderCard,
QFrame#alchemySimulationCard, QFrame#alchemySimulationResultCard,
QFrame#alchemySimulationResultsGroup {{
  background: {card};
  border: 1px solid {border};
  border-radius: 12px;
}}
QFrame#marketCard:hover, QFrame#metricCard:hover,
QFrame#alchemySimulationCard:hover, QFrame#alchemySimulationResultCard:hover {{
  background: {card_hover};
  border-color: {border_strong};
}}
QScrollArea#platformScrollArea {{
  background: transparent;
  border: 0;
}}
QScrollArea#platformScrollArea > QWidget > QWidget {{
  background: palette(window);
}}
QPushButton#platformModeButton {{
  min-height: 34px;
  padding: 0 16px;
  color: {muted};
  background: {input};
  border: 1px solid {border};
  border-radius: 8px;
  font-weight: 700;
}}
QPushButton#platformModeButton:hover {{
  color: {text};
  background: {card_hover};
}}
QPushButton#platformModeButton[active="true"] {{
  color: {cyan};
  background: {cyan_dim};
  border-color: {border_strong};
}}
QFrame#marketAccountRow {{
  background: {surface_alt};
  border: 0;
  border-bottom: 1px solid {border};
}}
QFrame#marketAccountRow:hover {{
  background: {card_hover};
}}
QLabel#marketAccountName, QLabel#platformFieldLabel {{
  color: {text};
  font-weight: 700;
}}
QLabel#marketLoginState {{
  min-height: 32px;
  padding: 0 12px;
  text-align: left;
  background: {input};
  color: {muted};
  border: 1px solid {border};
  border-radius: 7px;
  font-size: 11px;
  font-weight: 650;
}}
QLabel#marketLoginState[state="confirmed"] {{
  color: {lime};
  background: {lime_dim};
  border-color: {lime};
}}
QLabel#marketLoginState[state="unknown"] {{
  color: {amber};
  background: {amber_dim};
  border-color: {amber};
}}
QLabel#marketLoginState[state="missing"] {{
  color: {muted};
  background: {input};
  border-color: {border};
}}
QPushButton#marketOpenButton {{
  min-height: 32px;
  color: {primary_text};
  background: {blue};
  border: 1px solid {blue};
  border-radius: 7px;
  font-weight: 750;
}}
QPushButton#marketOpenButton:hover {{
  color: {primary_text};
  background: {cyan};
  border-color: {cyan};
}}
QPushButton#marketCollectButton {{
  min-height: 32px;
  color: {primary_text};
  background: {cyan};
  border: 1px solid {cyan};
  border-radius: 7px;
  font-weight: 750;
}}
QPushButton#marketCollectButton:hover {{
  color: {primary_text};
  background: {cyan_hover};
  border-color: {cyan_hover};
}}
QPushButton#marketCollectButton:pressed {{
  color: {primary_text};
  background: {cyan};
  border-color: {cyan_hover};
  padding-top: 1px;
}}
QPushButton#marketCollectButton:disabled {{
  color: {dim};
  background: {card};
  border-color: {border};
}}
QPushButton#dangerOutlineButton {{
  min-height: 34px;
  color: {rose};
  background: {rose_dim};
  border: 1px solid {rose};
  border-radius: 8px;
  font-weight: 700;
}}
QFrame#platformMaterialsPanel {{
  background: {surface};
  border: 1px solid {border};
  border-radius: 12px;
}}
QFrame#platformMaterialsEmpty {{
  background: {surface_alt};
  border: 1px dashed {border_strong};
  border-radius: 12px;
}}
QLabel#platformMaterialsEmptyTitle {{
  color: {text};
  font-size: 16px;
  font-weight: 750;
}}
QLabel#platformMaterialsEmptyHint {{
  color: {muted};
  font-size: 13px;
}}
QPushButton#primaryButton[emphasized="true"],
QPushButton#recipeLoadButton[emphasized="true"],
QPushButton#collectionImportButton[emphasized="true"],
QPushButton#marketCollectButton[emphasized="true"] {{
  border-color: {cyan_hover};
  background: {cyan};
  color: {primary_text};
}}
QPushButton#recipeLoadButton,
QPushButton#collectionImportButton {{
  min-height: 36px;
  padding: 0 16px;
  color: {primary_text};
  background: {cyan};
  border: 1px solid {cyan};
  border-radius: 10px;
  font-weight: 750;
}}
QPushButton#recipeLoadButton:hover,
QPushButton#collectionImportButton:hover {{
  color: {primary_text};
  background: {cyan_hover};
  border-color: {cyan_hover};
  padding: 0 16px 0 18px;
}}
QPushButton#recipeLoadButton:pressed,
QPushButton#collectionImportButton:pressed {{
  color: {primary_text};
  background: {cyan};
  border-color: {cyan_hover};
  padding: 1px 15px 0 17px;
}}
QPushButton#recipeLoadButton:disabled,
QPushButton#collectionImportButton:disabled {{
  color: {dim};
  background: {card};
  border-color: {border};
}}
QFrame#recipeBridgeSummary, QFrame#recipeBridgeMaterial {{
  background: {card};
  border: 1px solid {border};
  border-radius: 12px;
}}
QFrame#recipeBridgeSummary {{
  background: {surface_alt};
  border-color: {border_strong};
}}
QFrame#recipeBridgeMaterial:hover {{
  background: {card_hover};
  border-color: {border_strong};
}}
QLabel#recipeBridgeMaterialTitle {{
  color: {text};
  font-size: 14px;
  font-weight: 750;
}}
QLabel#recipeBridgeMaterialAltTitle {{
  color: {muted};
  font-size: 12px;
  font-weight: 650;
}}
QLabel#recipeBridgeCount {{
  color: {cyan};
  background: {cyan_dim};
  border: 1px solid {border_strong};
  border-radius: 9px;
  padding: 3px 9px;
  font-weight: 800;
}}
QLabel#recipeBridgeAltBadge {{
  color: {muted};
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 9px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 700;
}}
QLabel#recipeBridgeWear {{
  color: {cyan};
  font-size: 12px;
  font-weight: 700;
}}
QFrame#recipeBridgeMaterialAlt {{
  background: {surface};
  border: 1px solid {border};
  border-radius: 14px;
}}
QFrame#recipeBridgeMaterialAlt:hover {{
  background: {card_hover};
  border-color: {border_strong};
}}
QPushButton#recipeBridgePlatformButton {{
  min-height: 31px;
  padding: 0 10px;
  background: {input};
  border-color: {border};
  font-weight: 650;
}}
QPushButton#recipeBridgePlatformButton:hover {{
  color: {cyan};
  background: {cyan_dim};
  border-color: {cyan};
}}
QFrame#alchemyStep1CountCard {{
  background: {surface_alt};
  border-color: {border_strong};
}}
QFrame#alchemyGroupHeader {{
  background: {surface_alt};
  border: 0;
  border-radius: 10px;
}}
QFrame#alchemyTableFrame {{
  background: {input};
  border: 1px solid {border};
  border-radius: 10px;
}}
QWidget#alchemyProductRow {{
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 8px;
}}
QWidget#alchemyProductRow[inactive="true"] {{
  background: {input};
  border-color: {border};
}}
QWidget#alchemyProductRow[sw_selected="true"] {{
  background: {cyan_dim};
  border-color: {cyan};
}}
QWidget#fetchCustomEntryRow {{
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 10px;
}}

/* ---------- Buttons ---------- */
QPushButton {{
  min-height: 34px;
  padding: 0 14px;
  color: {text};
  background: {card};
  border: 1px solid {border};
  border-radius: 9px;
  font-weight: 650;
}}
QPushButton:hover {{
  color: {cyan};
  background: {card_hover};
  border-color: {cyan};
}}
QPushButton:pressed {{
  color: {cyan_hover};
  background: {cyan_dim};
  border-color: {cyan_hover};
}}
QPushButton:disabled {{
  color: {dim};
  background: {input};
  border-color: {border};
}}
QPushButton#primaryButton, QPushButton#alchemySelectFileBtn,
QPushButton#alchemyCalcBtn, QPushButton#alchemySimulationCalcBtn,
QPushButton#alchemySimulationSaveRecipeBtn, QPushButton#alchemyNextBtn,
QPushButton#loginSubmitBtn, QPushButton#confirmDialogOkBtn,
QPushButton#importToAlchemyMergeBtn, QPushButton#specialWearComplexityContinueBtn {{
  color: {primary_text};
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {cyan}, stop:1 {blue}
  );
  border-color: {cyan};
  font-weight: 800;
}}
QPushButton#primaryButton:hover, QPushButton#alchemySelectFileBtn:hover,
QPushButton#alchemyCalcBtn:hover, QPushButton#alchemySimulationCalcBtn:hover,
QPushButton#alchemySimulationSaveRecipeBtn:hover, QPushButton#alchemyNextBtn:hover,
QPushButton#loginSubmitBtn:hover, QPushButton#confirmDialogOkBtn:hover {{
  color: {primary_text};
  border-color: {cyan_hover};
}}
QPushButton#alchemyCalcBtn[calcStopping="true"] {{
  color: {amber};
  background: {amber_dim};
  border-color: {amber};
}}
QPushButton#alchemyClearFileBtn, QPushButton#alchemySimulationClearBtn,
QPushButton#importToAlchemyReplaceBtn {{
  color: {rose};
  background: {rose_dim};
  border-color: {rose};
}}
QPushButton#alchemyClearFileBtn:hover, QPushButton#alchemySimulationClearBtn:hover,
QPushButton#importToAlchemyReplaceBtn:hover {{
  color: {text};
  background: {rose};
}}
QPushButton#confirmDialogCancelBtn, QPushButton#specialWearComplexityThinkAgainBtn {{
  color: {muted};
  background: {input};
  border-color: {border};
  font-weight: 650;
}}
QPushButton#confirmDialogCancelBtn:hover,
QPushButton#specialWearComplexityThinkAgainBtn:hover {{
  color: {text};
  background: {card_hover};
  border-color: {border_strong};
}}
QPushButton#dangerButton {{
  color: {rose};
  background: transparent;
  border-color: {rose};
}}
QPushButton#dangerButton:hover {{
  color: {text};
  background: {rose};
}}
QPushButton#alchemyExcludeRecipeBtn {{
  color: {amber};
  background: {amber_dim};
  border-color: {amber};
}}
QPushButton#purchaseBatchReconcileBtn {{
  min-height: 40px;
  max-height: 40px;
  color: #FFFFFF;
  background: #16A34A;
  border-color: #16A34A;
  font-weight: 800;
}}
QPushButton#purchaseBatchReconcileBtn:hover {{
  color: #FFFFFF;
  background: #15803D;
  border-color: #15803D;
}}
QPushButton#purchaseBatchReconcileBtn:pressed {{
  color: #FFFFFF;
  background: #166534;
  border-color: #166534;
  padding-top: 1px;
}}
QPushButton#purchaseBatchViewMaterialsBtn {{
  min-height: 40px;
  max-height: 40px;
  color: {primary_text};
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {cyan}, stop:1 {blue}
  );
  border-color: {cyan};
  font-weight: 800;
}}
QPushButton#purchaseBatchViewMaterialsBtn:hover {{
  color: {primary_text};
  border-color: {cyan_hover};
}}
QPushButton#purchaseBatchViewMaterialsBtn:pressed {{
  color: {primary_text};
  border-color: {cyan_hover};
  padding-top: 1px;
}}
QPushButton#alchemyResetWearBtn, QPushButton#alchemyStep2WearNoticeBtn {{
  color: {muted};
  background: transparent;
}}
QPushButton#alchemyResetWearBtn:hover, QPushButton#alchemyStep2WearNoticeBtn:hover {{
  color: {cyan};
  background: {cyan_dim};
}}
QPushButton#fetchCustomEntryRemoveBtn, QPushButton#purchaseQrNavBtn,
QPushButton#purchaseActionIconBtn {{
  min-width: 30px;
  max-width: 30px;
  min-height: 30px;
  padding: 0;
  background: transparent;
}}
QPushButton#purchaseActionIconBtn[selected="true"] {{
  color: {cyan};
  background: {cyan_dim};
  border-color: {cyan};
}}

/* ---------- Segmented controls ---------- */
QWidget#alchemyStep2ModeSegmented {{
  background: {input};
  border: 1px solid {border};
  border-radius: 10px;
}}
QFrame#alchemyStep2ModeSlider {{
  background: {cyan_dim};
  border: 1px solid {cyan};
  border-radius: 8px;
}}
QPushButton#alchemySegmentLeft, QPushButton#alchemySegmentMiddle,
QPushButton#alchemySegmentRight {{
  min-height: 34px;
  padding: 0 13px;
  color: {muted};
  background: transparent;
  border: 0;
  border-radius: 8px;
}}
QPushButton#alchemySegmentLeft:hover, QPushButton#alchemySegmentMiddle:hover,
QPushButton#alchemySegmentRight:hover,
QPushButton#alchemySegmentLeft:checked, QPushButton#alchemySegmentMiddle:checked,
QPushButton#alchemySegmentRight:checked {{
  color: {cyan};
  background: transparent;
}}

/* ---------- Inputs ---------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
  min-height: 34px;
  padding: 0 10px;
  color: {text};
  background: {input};
  border: 1px solid {border};
  border-radius: 9px;
  selection-background-color: {cyan};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
  border-color: {border_strong};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
  background: {surface_alt};
  border-color: {cyan};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
  color: {dim};
  background: {input};
  border-color: {border};
}}
QLineEdit[inactive_field="true"] {{
  color: {dim};
  background: {input};
}}
QLineEdit[sw_line_active="true"] {{
  border-color: {cyan};
}}
QComboBox::drop-down {{
  width: 26px;
  border: 0;
}}
QComboBox QAbstractItemView {{
  color: {text};
  background: {surface};
  border: 1px solid {border_strong};
  border-radius: 8px;
  outline: 0;
  selection-background-color: {cyan_dim};
  selection-color: {cyan};
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
  width: 18px;
  background: transparent;
  border: 0;
}}
QSpinBox#customItemQuantitySpin {{
  padding-right: 24px;
}}
QSpinBox#customItemQuantitySpin::up-button,
QSpinBox#customItemQuantitySpin::down-button {{
  width: 22px;
  border-left: 1px solid {border};
}}
QCheckBox {{
  color: {text};
  spacing: 7px;
  background: transparent;
}}
QCheckBox::indicator {{
  width: 16px;
  height: 16px;
  background: {input};
  border: 1px solid {border_strong};
  border-radius: 4px;
}}
QCheckBox::indicator:hover {{
  border-color: {cyan};
}}
QCheckBox::indicator:checked {{
  image: url({check_icon});
  background: {cyan};
  border-color: {cyan};
}}
QCheckBox::indicator:disabled {{
  background: {surface_alt};
  border-color: {border};
}}
QRadioButton {{
  spacing: 7px;
  background: transparent;
}}
QRadioButton::indicator {{
  width: 15px;
  height: 15px;
  background: {input};
  border: 1px solid {border_strong};
  border-radius: 8px;
}}
QRadioButton::indicator:checked {{
  background: {cyan};
  border: 4px solid {cyan_dim};
}}

/* ---------- Tables and lists ---------- */
QTableWidget, QListWidget, QTreeWidget {{
  color: {text};
  background: transparent;
  border: 0;
  outline: 0;
  alternate-background-color: {surface_alt};
}}
QTableWidget#alchemyTable, QTableWidget#alchemyRecipeProductTable,
QTableWidget#alchemySubstratePickTable, QTreeWidget#excludeRecipeListWidget {{
  background: {input};
  border: 1px solid {border};
  border-radius: 8px;
}}
QTableWidget::item {{
  padding: 7px;
  border: 0;
  border-bottom: 1px solid {border};
}}
QTableWidget::item:hover {{
  background: {card_hover};
}}
QTableWidget::item:selected {{
  color: {cyan};
  background: {cyan_dim};
}}
QHeaderView::section {{
  min-height: 28px;
  padding: 6px 8px;
  color: {muted};
  background: {surface_alt};
  border: 0;
  border-bottom: 1px solid {border};
  font-weight: 700;
}}
QFrame#collectionPresetSidebar {{
  background: {surface};
  border: 1px solid {border};
  border-radius: 12px;
}}
QListWidget#collectionPresetList {{
  background: transparent;
  border: 0;
  font-size: 14px;
}}
QListWidget#collectionPresetList::item {{
  min-height: 34px;
  padding: 8px 10px;
  margin: 2px 0;
  border-radius: 8px;
}}
QListWidget#collectionPresetList::item:hover {{
  background: {card_hover};
}}
QListWidget#collectionPresetList::item:selected {{
  color: {cyan};
  background: {cyan_dim};
}}
QFrame#collectionPresetItemCard {{
  background: {card};
  border: 1px solid {border};
  border-radius: 12px;
}}
QLabel#collectionPresetItemTitle {{
  font-size: 15px;
  font-weight: 700;
}}
QListWidget#recipeFolderList {{
  background: transparent;
  font-size: 15px;
}}
QListWidget#recipeFolderList::item {{
  min-height: 34px;
  padding: 8px 10px;
  margin: 2px 0;
  border-radius: 8px;
  font-size: 15px;
}}
QListWidget#recipeFolderList::item:hover {{
  background: {card_hover};
}}
QListWidget#recipeFolderList::item:selected {{
  color: {cyan};
  background: {cyan_dim};
}}
QLineEdit#recipeFolderListInlineEdit {{
  font-size: 15px;
  padding: 2px 6px;
  border: 1px solid {cyan};
  border-radius: 6px;
  background: {input};
  color: {text};
}}
QLabel#recipeFolderHeading {{
  font-size: 13px;
}}
QFrame#recipeFolderInsertLine {{
  background: {cyan};
  border: 0;
}}
QListWidget#inventoryList {{
  background: transparent;
}}
QListWidget#inventoryList::item {{
  padding: 0;
  margin: 0;
  background: transparent;
  border: 0;
}}

/* ---------- Feature cards ---------- */
QWidget#inventoryItemCard {{
  background: {card};
  border: 1px solid {border};
  border-radius: 12px;
}}
QWidget#inventoryItemCard:hover {{
  background: {card_hover};
  border-color: {border_strong};
}}
QWidget#inventoryItemCard[selected="true"] {{
  background: {cyan_dim};
  border-color: {cyan};
}}
QLabel#inventoryCardName {{
  color: {text};
  font-size: 12px;
  font-weight: 700;
}}
QLabel#inventoryCardWear, QLabel#inventoryCardStatus,
QLabel#alchemySimulationSubstratePriceLine {{
  color: {muted};
  font-size: 11px;
}}
QLabel#inventoryTotalValue {{
  color: {text};
  font-size: 15px;
  font-weight: 800;
  padding: 2px 14px;
}}
QLabel#alchemySimulationResultPriceLine {{
  color: {muted};
  font-size: 15px;
  font-weight: 800;
}}
QLabel#alchemySimulationResultPriceLine[priceOutcome="profit"] {{
  color: #10b981;
}}
QLabel#alchemySimulationResultPriceLine[priceOutcome="loss"] {{
  color: #ef4444;
}}
QLabel#alchemySimulationResultProb {{
  color: {cyan};
  font-size: 14px;
  font-weight: 800;
}}
QFrame#alchemySimulationResultsDivider {{
  background: {border_strong};
  border: 0;
}}
QLabel#recipeSavedTitle {{
  color: {cyan};
  font-size: 15px;
  font-weight: 750;
}}
QWidget#recipeManageToolbar {{
  background: {surface_alt};
  border: 1px solid {border};
  border-radius: 10px;
}}
QWidget#recipeManageRow {{
  background: {surface};
  border: 1px solid {border};
  border-radius: 10px;
}}
QWidget#recipeManageRow:hover {{
  background: {surface_alt};
  border-color: {border_strong};
}}
QWidget#recipeManageBody, QWidget#recipeBatchPrimarySlot,
QWidget#alchemyGroupsContainer, QWidget#alchemySimulationScrollInner,
QWidget#alchemySimulationGridHost, QWidget#alchemySimulationResultsGridHost,
QWidget#alchemySimulationResultsGroupGridHost {{
  background: transparent;
  border: 0;
}}
QWidget#fetchWeaponBoxCandidateRow:hover {{
  background: {cyan_dim};
}}
QWidget#fetchWeaponBoxCandidateRow,
QWidget#fetchWeaponBoxCandidateChips,
QLabel#fetchWeaponBoxCandidateName {{
  background: transparent;
  border: 0;
  padding: 0;
  margin: 0;
}}
QLabel#fetchWeaponBoxCandidateName {{
  color: {text};
  font-weight: 650;
}}
QFrame#alchemyWearIeeeFrame {{
  background: {surface};
  border: 1px solid {cyan};
  border-radius: 8px;
}}
QLabel#alchemyWearIeeeLabel {{
  color: {muted};
}}

/* ---------- Scroll areas and scrollbars ---------- */
QScrollArea#alchemyScrollArea, QScrollArea#alchemySimulationScroll,
QScrollArea#alchemySimulationResultsHeaderScroll,
QScrollArea#moveRecipeFolderScroll {{
  background: transparent;
  border: 0;
}}
QScrollBar:vertical {{
  width: 10px;
  margin: 2px;
  background: transparent;
}}
QScrollBar::handle:vertical {{
  min-height: 34px;
  background: {dim};
  border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
  background: {muted};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
  height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
  background: transparent;
}}
QScrollBar:horizontal {{
  height: 10px;
  margin: 2px;
  background: transparent;
}}
QScrollBar::handle:horizontal {{
  min-width: 34px;
  background: {dim};
  border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
  width: 0;
}}

/* ---------- Progress and feedback ---------- */
QProgressBar#alchemyCalcProgressBar {{
  min-height: 8px;
  max-height: 8px;
  color: transparent;
  background: {input};
  border: 1px solid {border};
  border-radius: 4px;
}}
QProgressBar#alchemyCalcProgressBar::chunk {{
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {blue}, stop:1 {cyan}
  );
  border-radius: 3px;
}}
QProgressBar#collectionProgressBar {{
  min-height: 14px;
  max-height: 14px;
  color: transparent;
  background: {input};
  border: 1px solid {border};
  border-radius: 7px;
}}
QProgressBar#collectionProgressBar::chunk {{
  background: qlineargradient(
    x1:0, y1:0, x2:1, y2:0,
    stop:0 {blue}, stop:1 {cyan}
  );
  border-radius: 6px;
}}
QLabel#alchemyCalcProgressLabel {{
  color: {cyan};
  font-weight: 800;
}}
QLabel#collectionProgressLabel {{
  color: {cyan};
  font-weight: 800;
  font-size: 13px;
}}
QLabel#alchemyCalcProgressDetailLabel {{
  color: {muted};
}}
QLabel#muted[collectionState="complete"] {{
  color: {lime};
  font-weight: 700;
}}
QFrame#toastWidget, QFrame#toastWidgetSuccess,
QFrame#toastWidgetError, QFrame#toastWidgetWarning {{
  padding: 12px 24px;
  background: {surface};
  border: 1px solid {cyan};
  border-radius: 10px;
}}
QFrame#toastWidgetSuccess {{
  background: {lime_dim};
  border-color: {lime};
}}
QFrame#toastWidgetError {{
  background: {rose_dim};
  border-color: {rose};
}}
QFrame#toastWidgetWarning {{
  background: {amber_dim};
  border-color: {amber};
}}
QFrame#accessLockBanner {{
  background: {amber_dim};
  border: 0;
  border-bottom: 1px solid {amber};
}}
QLabel#accessLockBannerLabel {{
  color: {text};
  font-size: 13px;
  font-weight: 650;
}}
QPushButton#accessLockBannerBtn {{
  min-height: 30px;
  padding: 0 14px;
  color: {primary_text};
  background: {cyan};
  border: 1px solid {cyan};
  border-radius: 8px;
  font-weight: 750;
}}
QPushButton#accessLockBannerBtn:hover {{
  background: {cyan_hover};
  border-color: {cyan_hover};
}}

/* ---------- Dialogs and popups ---------- */
QDialog {{
  background: {bg};
}}
QWidget#alertOverlay, QWidget#excludeRecipeOverlay, QWidget#loginOverlay,
QWidget#settingsDialogBackdrop {{
  background: {overlay};
}}
QFrame#loginBox, QFrame#wearNoticeBox {{
  background: {surface};
  border: 1px solid {border_strong};
  border-radius: 16px;
}}
QLabel#loginTitle {{
  color: {text};
  font-size: 18px;
  font-weight: 800;
}}
QLabel#purchaseQrHint {{
  color: {muted};
  font-size: 12px;
}}
QLabel#purchaseQrSubstrate {{
  color: {blue};
  font-size: 16px;
  font-weight: 700;
  padding: 4px 2px;
}}
QLabel#purchaseQrSubstrateMeta {{
  color: {dim};
  font-size: 12px;
}}
QLabel#loginError {{
  color: {rose};
}}
QLabel#loginFormLabel {{
  color: {muted};
  font-weight: 650;
}}
QLabel#alertDialogMessage, QLabel#wearNoticeMessage {{
  color: {muted};
  font-size: 13px;
}}
QLineEdit#loginInput {{
  min-height: 36px;
  padding: 0 12px;
  background: {input};
  border: 1px solid {border};
  border-radius: 10px;
}}
QLineEdit#loginInput:focus {{
  border-color: {cyan};
}}
QMenu {{
  padding: 6px;
  color: {text};
  background: {surface};
  border: 1px solid {border_strong};
  border-radius: 9px;
}}
QMenu::item {{
  min-height: 26px;
  padding: 5px 18px;
  border-radius: 6px;
}}
QMenu::item:selected {{
  color: {cyan};
  background: {cyan_dim};
}}
QMenu::separator {{
  height: 1px;
  margin: 5px 8px;
  background: {border};
}}
QToolTip {{
  padding: 6px 8px;
  color: {text};
  background: {surface};
  border: 1px solid {border_strong};
  border-radius: 6px;
}}
QMessageBox {{
  background: {surface};
}}
""".format(**c)


def apply_theme(app: QApplication | None, name: str) -> None:
    if app is None:
        return
    colors = PALETTES.get(name, PALETTES["dark"])
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(colors["bg"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(colors["input"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["surface_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(colors["card"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(colors["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(colors["cyan"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["primary_text"]))
    palette.setColor(QPalette.ColorRole.Light, QColor(colors["border"]))
    palette.setColor(QPalette.ColorRole.Midlight, QColor(colors["border_strong"]))
    palette.setColor(QPalette.ColorRole.Mid, QColor(colors["dim"]))
    palette.setColor(QPalette.ColorRole.Dark, QColor(colors["muted"]))
    palette.setColor(QPalette.ColorRole.Shadow, QColor(colors["cyan_dim"]))
    palette.setColor(QPalette.ColorRole.Link, QColor(colors["cyan_hover"]))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(colors["amber"]))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(colors["rose"]))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["lime"]))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["dim"]))
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.Text,
        QColor(colors["dim"]),
    )
    palette.setColor(
        QPalette.ColorGroup.Disabled,
        QPalette.ColorRole.ButtonText,
        QColor(colors["dim"]),
    )
    app.setPalette(palette)
    if not bool(app.property("cs2thAdaptiveStylesheetInstalled")):
        app.setStyleSheet(build_stylesheet(name))
        app.setProperty("cs2thAdaptiveStylesheetInstalled", True)

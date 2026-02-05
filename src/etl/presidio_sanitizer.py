try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    PRESIDIO_AVAILABLE = True
except ImportError:
    PRESIDIO_AVAILABLE = False

from utils.logger import logger

class PresidioSanitizer:
    """
    Advanced PII removal using Microsoft Presidio (NER-based).
    Catches PERSON, LOCATION, PHONE_NUMBER, etc.
    """
    def __init__(self, language="en"):
        self.language = language
        self._is_ready = False
        if PRESIDIO_AVAILABLE:
            try:
                # Note: Requires spacy models (e.g., en_core_web_lg) to be installed
                self.analyzer = AnalyzerEngine()
                self.anonymizer = AnonymizerEngine()
                self._is_ready = True
                logger.info(f"PresidioSanitizer initialized (Language: {language})")
            except Exception as e:
                logger.warning(f"Presidio engines failed to start (likely missing spaCy model): {e}")
                self.analyzer = None
                self.anonymizer = None
        else:
            self.analyzer = None
            self.anonymizer = None

    def sanitize(self, text: str) -> str:
        """Sanitize text using Presidio NER if available."""
        if not text or not self._is_ready:
            return text
            
        try:
            # Analyze for entities
            results = self.analyzer.analyze(
                text=text, 
                language=self.language,
                entities=["PERSON", "LOCATION", "PHONE_NUMBER", "EMAIL_ADDRESS", "CREDIT_CARD"]
            )
            # Anonymize found entities
            anonymized_result = self.anonymizer.anonymize(
                text=text, 
                analyzer_results=results
            )
            return anonymized_result.text
        except Exception as e:
            logger.debug(f"Presidio sanitization error: {e}")
            return text

    @property
    def is_active(self):
        return self._is_ready

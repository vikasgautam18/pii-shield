from pydantic import BaseModel, Field


class AnonymizeRequest(BaseModel):
    text: str = Field(..., description="The text to anonymize")
    language: str = Field(
        default="en", description="Language of the text (ISO 639-1 code)"
    )
    allow_list: list[str] = Field(
        default_factory=list,
        description="List of terms to exclude from anonymization (e.g. company names)",
    )
    entity_type_allow_list: list[str] = Field(
        default_factory=list,
        description="List of entity types to exclude from anonymization (e.g. EMAIL_ADDRESS)",
    )


class AnonymizeUniqueResponse(BaseModel):
    id: str = Field(..., description="Unique session ID for later de-anonymization")
    text: str = Field(..., description="The original input text")
    anonymized_text: str = Field(
        ..., description="Text with PII replaced by unique identifiers"
    )
    entity_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from unique placeholder to original value",
    )
    hash_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from SHA3-256 hash to original value (for hashed entities)",
    )
    encrypt_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping from encrypted token (PQC or Fernet) to original value (for encrypted entities)",
    )


class DeanonymizeRequest(BaseModel):
    id: str = Field(
        ..., description="Session ID returned by /anonymize_unique"
    )
    text: str = Field(
        ...,
        description="Text containing placeholders to replace with original PII values",
    )
    include_hashed: bool = Field(
        default=False,
        description="If true, also restore hashed PII values using the stored hash mapping",
    )
    include_encrypted: bool = Field(
        default=False,
        description="If true, also restore encrypted PII values using the stored encrypt mapping",
    )


class DeanonymizeResponse(BaseModel):
    text: str = Field(..., description="Text with placeholders restored to original values")
    id: str = Field(..., description="The session ID that was used")


class RegisterAppRequest(BaseModel):
    app_name: str = Field(..., description="Name of the application to register")


class RegisterAppResponse(BaseModel):
    app_id: str = Field(..., description="Unique application ID")
    app_name: str = Field(..., description="Name of the registered application")
    config: dict[str, str] = Field(
        default_factory=dict,
        description="Per-entity anonymization strategy config",
    )


class AppConfigUpdateRequest(BaseModel):
    entity_type: str = Field(..., description="Entity type to configure")
    strategy: str = Field(..., description="Anonymization strategy: 'replace', 'hash', 'encrypt', or 'fake'")


class AppDetailResponse(BaseModel):
    app_id: str = Field(..., description="Unique application ID")
    app_name: str = Field(..., description="Name of the registered application")
    config: dict[str, str] = Field(
        default_factory=dict,
        description="Per-entity anonymization strategy config",
    )

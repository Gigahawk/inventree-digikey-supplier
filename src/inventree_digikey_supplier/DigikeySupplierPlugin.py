"""Sample supplier plugin."""

from django.conf import settings
from digikey_api_v4.api import DigikeyClient
from digikey_api_v4.models import KeywordRequest
from bravado.exception import HTTPNotFound

from company.models import Company, ManufacturerPart, SupplierPart, SupplierPriceBreak
from part.models import Part
from plugin.mixins import SupplierMixin, supplier
from plugin.plugin import InvenTreePlugin


class DigikeySupplierPlugin(SupplierMixin, InvenTreePlugin):
    """Example plugin to integrate with a dummy supplier."""

    NAME = "DigikeySupplierPlugin"
    SLUG = "digikeysupplier"
    TITLE = "DigiKey supplier plugin"

    VERSION = "0.0.2"

    SETTINGS = {
        "DOWNLOAD_IMAGES": {
            "name": "Download part images",
            "description": "Enable downloading of part images during import (not recommended during testing)",
            "validator": bool,
            "default": False,
        },
        "CLIENT_ID": {
            "name": "Client ID",
            "description": "DigiKey API V4 Client ID",
            "default": "",
            "reqiured": True,
        },
        "CLIENT_SECRET": {
            "name": "Client Secret",
            "description": "DigiKey API V4 Client Secret",
            "default": "",
            "reqiured": True,
        },
    }

    def __init__(self):
        """Initialize the sample supplier plugin."""
        super().__init__()

    @property
    def _client(self):
        return DigikeyClient(
            client_id=self.get_setting("CLIENT_ID"),
            client_secret=self.get_setting("CLIENT_SECRET"),
            sandbox=False,
        )

    def get_suppliers(self) -> list[supplier.Supplier]:
        """Return a list of available suppliers."""
        return [supplier.Supplier(slug="digikey", name="DigiKey")]

    def get_search_results(
        self, supplier_slug: str, term: str
    ) -> list[supplier.SearchResult]:
        """Return a list of search results based on the search term."""
        results = self._client.keyword_search(body=KeywordRequest(Keywords=term))
        products = results.Products
        return [
            supplier.SearchResult(
                # TODO: In theory MPN may not be unique across manufacturers.
                # Unfortunately it doesn't really seem like DigiKey offers a
                # guaranteed unique identifier per part other than the DigiKey
                # part number but there are usually a few depending on packaging
                # options
                sku=p.ManufacturerProductNumber,
                name=p.Description.ProductDescription,
                exact=p.ManufacturerProductNumber == term,
                description=p.Description.DetailedDescription,
                # TODO: use locale price to format this?
                # Don't format to 2 decimal places, real parts may cost less than 1c
                price=f"${p.UnitPrice}",
                link=p.ProductUrl,
                image_url=p.PhotoUrl,
                existing_part=getattr(
                    SupplierPart.objects.filter(
                        SKU=p.ManufacturerProductNumber
                    ).first(),
                    "part",
                    None,
                ),
            )
            for p in products
        ]

    def get_import_data(self, supplier_slug: str, part_id: str):
        """Return import data for a specific part ID."""
        try:
            return self._client.product_details(part_id).Product
        except HTTPNotFound:
            raise supplier.PartNotFoundError()
        raise supplier.PartNotFoundError()

    def get_pricing_data(self, data) -> dict[int, tuple[float, str]]:
        """Return pricing data for the given part data."""
        # DigiKey has different breaks depending on variation.
        # Just use the lowest price listed for identical breaks
        # TODO: use locale or something to format dollar amount

        out = {}
        for pv in data.ProductVariations:
            for pb in pv.StandardPricing:
                qty = int(pb.BreakQuantity)
                price = float(pb.UnitPrice)
                existing = out.get(qty, (999999, "CAD"))
                if existing[0] > price:
                    out[qty] = (price, "CAD")
        return out

    def get_parameters(self, data) -> list[supplier.ImportParameter]:
        """Return a list of parameters for the given part data."""
        return [
            supplier.ImportParameter(
                name=p.ParameterText,
                value=p.ValueText,
            )
            for p in data.Parameters
        ]

    def import_part(self, data, **kwargs) -> Part:
        """Import a part based on the provided data."""
        part, created = Part.objects.get_or_create(
            name__iexact=data.ManufacturerProductNumber,
            purchaseable=True,
            defaults={
                "name": data.Description.ProductDescription,
                "description": data.Description.DetailedDescription,
                "link": data.ProductUrl,
                **kwargs,
            },
        )

        # If the part was created, set additional fields
        if created:
            # Prevent downloading images during testing, as this can lead to unreliable tests
            if (
                data.PhotoUrl
                and not settings.TESTING
                and self.get_setting("DOWNLOAD_IMAGES")
            ):
                file, fmt = self.download_image(data.PhotoUrl)
                filename = f"part_{part.pk}_image.{fmt.lower()}"
                part.image.save(filename, file)

            # TODO: What does variants mean in this context?
            # I don't think it means the same thing as ProductVariations
            ## link other variants if they exist in our inventree database
            # if len(data["variants"]):
            #    # search for other parts that may already have a template part associated
            #    variant_parts = [
            #        x.part
            #        for x in SupplierPart.objects.filter(SKU__in=data["variants"])
            #    ]
            #    parent_part = self.get_template_part(
            #        variant_parts,
            #        {
            #            # we cannot extract a real name for the root part, but we can try to guess a unique name
            #            "name": data["sku"].replace(data["material"] + "-", ""),
            #            "description": data["name"].replace(" " + data["material"], ""),
            #            "link": data["link"],
            #            "image": part.image.name,
            #            "is_template": True,
            #            **kwargs,
            #        },
            #    )

            #    # after the template part was created, we need to refresh the part from the db because its tree id may have changed
            #    # which results in an error if saved directly
            #    part.refresh_from_db()
            #    part.variant_of = parent_part
            #    part.save()

        return part

    def import_manufacturer_part(self, data, **kwargs) -> ManufacturerPart:
        """Import a manufacturer part based on the provided data."""
        mft, _ = Company.objects.get_or_create(
            name__iexact=data.Manufacturer.Name,
            defaults={
                "is_manufacturer": True,
                "is_supplier": False,
                "name": data.Manufacturer.Name,
            },
        )

        mft_part, created = ManufacturerPart.objects.get_or_create(
            MPN=f"MAN-{data.ManufacturerProductNumber}", manufacturer=mft, **kwargs
        )

        if created:
            # Attachments, notes, parameters and more can be added here
            # TODO: Add datasheet link or something?
            pass

        return mft_part

    def import_supplier_part(self, data, **kwargs) -> SupplierPart:
        """Import a supplier part based on the provided data."""
        spp, _ = SupplierPart.objects.get_or_create(
            SKU=data.ManufacturerProductNumber,
            supplier=self.supplier_company,
            **kwargs,
            defaults={"link": data.ProductUrl},
        )

        SupplierPriceBreak.objects.filter(part=spp).delete()
        SupplierPriceBreak.objects.bulk_create(
            [
                SupplierPriceBreak(
                    part=spp, quantity=quantity, price=price, price_currency=currency
                )
                for quantity, (price, currency) in self.get_pricing_data(data).items()
            ]
        )

        return spp

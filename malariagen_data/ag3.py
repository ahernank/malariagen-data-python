import os
import pandas
from fsspec.core import url_to_fs
import zarr
import dask.array as da
import numpy as np
from .util import read_gff3, unpack_gff3_attributes, SafeStore
from . import veff
import allel

public_releases = ("v3",)
gff3_path = (
    "reference/genome/agamp4/Anopheles-gambiae-PEST_BASEFEATURES_AgamP4.12.gff3.gz"
)


def _path_to_url(fs, root_path, path):
    protocol = fs.protocol
    if isinstance(protocol, tuple):
        protocol = protocol[0]
    joined_path = os.path.join(root_path, path)
    url = f"{protocol}://{joined_path}"
    return url


class Ag3:
    """Provides access to data from Ag 3 releases.

    Parameters
    ----------
    url : str
        Base path to data. Give "gs://vo_agam_release/" to use Google Cloud Storage,
        or a local path on your file system if data have been downloaded.
    **kwargs
        Passed through to fsspec when setting up file system access.

    Examples
    --------
    Access data from Google Cloud Storage:

        >>> import malariagen_data
        >>> ag3 = malariagen_data.Ag3("gs://vo_agam_release/")

    Access data downloaded to a local file system:

        >>> ag3 = malariagen_data.Ag3("/local/path/to/vo_agam_release/")

    """

    def __init__(self, url, **kwargs):

        # special case Google Cloud Storage, use anonymous access, avoids a delay
        if url.startswith("gs://") or url.startswith("gcs://"):
            kwargs.setdefault("token", "anon")

        # process the url using fsspec
        pre = kwargs.pop("pre", False)
        fs, path = url_to_fs(url, **kwargs)
        self._fs = fs
        self._path = path

        # discover which releases are available
        sub_dirs = [p.split("/")[-1] for p in self._fs.ls(self._path)]
        releases = [d for d in sub_dirs if d.startswith("v3")]
        if not pre:
            releases = [d for d in releases if d in public_releases]
        if len(releases) == 0:
            raise ValueError(f"No releases found at location {url!r}")
        self._releases = releases

        # setup caches
        self._cache_sample_sets = dict()
        self._cache_general_metadata = dict()
        self._cache_species_calls = dict()
        self._cache_site_filters = dict()
        self._cache_snp_sites = None
        self._cache_snp_genotypes = dict()
        self._cache_genome = None
        self._cache_geneset = None
        self._cache_annotator = None

    def sample_sets(self, release="v3"):
        """Access the manifest of sample sets.

        Parameters
        ----------
        release : str
            Release identifier. Give "v3" to access the Ag1000G phase 3 data release.

        Returns
        -------
        df : pandas.DataFrame

        """

        if release not in self._releases:
            raise ValueError(f"Release not available: {release!r}")

        try:
            return self._cache_sample_sets[release]

        except KeyError:
            path = f"{self._path}/{release}/manifest.tsv"
            with self._fs.open(path) as f:
                df = pandas.read_csv(f, sep="\t", na_values="")
            df["release"] = release
            self._cache_sample_sets[release] = df
            return df

    @property
    def v3_wild(self):
        return [
            x
            for x in self.sample_sets(release="v3")["sample_set"].tolist()
            if x != "AG1000G-X"
        ]

    def _lookup_release(self, *, sample_set):
        # find which release this sample set was included in
        for release in self._releases:
            df_sample_sets = self.sample_sets(release=release)
            if sample_set in df_sample_sets["sample_set"].tolist():
                return release
        raise ValueError(f"No release found for sample set {sample_set!r}")

    def _read_general_metadata(self, *, sample_set):
        """Read metadata for a single sample set."""
        try:
            return self._cache_general_metadata[sample_set]
        except KeyError:
            release = self._lookup_release(sample_set=sample_set)
            path = (
                f"{self._path}/{release}/metadata/general/{sample_set}/samples.meta.csv"
            )
            with self._fs.open(path) as f:
                df = pandas.read_csv(f, na_values="")

            # add a couple of columns for convenience
            df["sample_set"] = sample_set
            df["release"] = release

            self._cache_general_metadata[sample_set] = df
            return df

    def _read_species_calls(self, *, sample_set, analysis, method):
        """Read species calls for a single sample set."""
        key = (sample_set, analysis, method)
        try:
            return self._cache_species_calls[key]
        except KeyError:
            release = self._lookup_release(sample_set=sample_set)
            path = (
                f"{self._path}/{release}/metadata/species_calls_{analysis}"
                f"/{sample_set}/samples.species_{method}.csv"
            )
            with self._fs.open(path) as f:
                df = pandas.read_csv(
                    f,
                    na_values="",
                    # ensure correct dtype even where all values are missing
                    dtype={
                        "species_gambcolu_arabiensis": object,
                        "species_gambiae_coluzzii": object,
                    },
                )

            # add a single species call column, for convenience
            df["species"] = np.array([np.nan] * len(df), dtype=object)
            loc = df["species_gambcolu_arabiensis"].values == "arabiensis"
            df.loc[loc, "species"] = "arabiensis"
            loc = df["species_gambcolu_arabiensis"].values == "intermediate"
            df.loc[loc, "species"] = "intermediate_arabiensis_gambiae"
            loc = (df["species_gambcolu_arabiensis"].values == "gamb_colu") & (
                df["species_gambiae_coluzzii"].values == "gambiae"
            )
            df.loc[loc, "species"] = "gambiae"
            loc = (df["species_gambcolu_arabiensis"].values == "gamb_colu") & (
                df["species_gambiae_coluzzii"].values == "coluzzii"
            )
            df.loc[loc, "species"] = "coluzzii"
            loc = (df["species_gambcolu_arabiensis"].values == "gamb_colu") & (
                df["species_gambiae_coluzzii"].values == "intermediate"
            )
            df.loc[loc, "species"] = "intermediate_gambiae_coluzzii"

            self._cache_species_calls[key] = df
            return df

    def _prep_sample_sets_arg(self, *, sample_sets):
        if sample_sets == "v3_wild":
            # convenience, special case to exclude crosses
            sample_sets = self.v3_wild

        elif isinstance(sample_sets, str) and sample_sets.startswith("v3"):
            # convenience, can use a release identifier to denote all sample sets
            # in a release
            sample_sets = self.sample_sets(release=sample_sets)["sample_set"].tolist()

        if not isinstance(sample_sets, (str, list, tuple)):
            raise TypeError(f"Invalid sample_sets: {sample_sets!r}")

        return sample_sets

    def species_calls(self, sample_sets="v3_wild", analysis="20200422", method="aim"):
        """Access species calls for one or more sample sets.

        Parameters
        ----------
        sample_sets : str or list of str
            Can be a sample set identifier (e.g., "AG1000G-AO") or a list of sample set
            identifiers (e.g., ["AG1000G-BF-A", "AG1000G-BF-B"] or a release identifier (e.g.,
            "v3") or a list of release identifiers.
        analysis : str
            Species calling analysis version.
        method : str
            Species calling method; "aim" is ancestry informative markers, "pca" is principal
            components analysis.

        Returns
        -------
        df : pandas.DataFrame

        """

        sample_sets = self._prep_sample_sets_arg(sample_sets=sample_sets)

        if isinstance(sample_sets, str):
            # assume single sample set
            df = self._read_species_calls(
                sample_set=sample_sets, analysis=analysis, method=method
            )

        else:
            # concatenate multiple sample sets
            dfs = [
                self.species_calls(sample_sets=c, analysis=analysis, method=method)
                for c in sample_sets
            ]
            df = pandas.concat(dfs, axis=0, sort=False).reset_index(drop=True)

        return df

    def sample_metadata(self, sample_sets="v3_wild", species_calls=("20200422", "aim")):
        """Access sample metadata for one or more sample sets.

        Parameters
        ----------
        sample_sets : str or list of str
            Can be a sample set identifier (e.g., "AG1000G-AO") or a list of sample set
            identifiers (e.g., ["AG1000G-BF-A", "AG1000G-BF-B"]) or a release identifier (e.g.,
            "v3") or a list of release identifiers.
        species_calls : (str, str), optional
            Include species calls in metadata.

        Returns
        -------
        df : pandas.DataFrame

        """

        sample_sets = self._prep_sample_sets_arg(sample_sets=sample_sets)

        if isinstance(sample_sets, str):
            # assume single sample set
            df = self._read_general_metadata(sample_set=sample_sets)
            if species_calls is not None:
                analysis, method = species_calls
                df_species = self._read_species_calls(
                    sample_set=sample_sets, analysis=analysis, method=method
                )
                df = df.merge(df_species, on="sample_id", sort=False)

        else:
            # concatenate multiple sample sets
            dfs = [
                self.sample_metadata(sample_sets=c, species_calls=species_calls)
                for c in sample_sets
            ]
            df = pandas.concat(dfs, axis=0, sort=False).reset_index(drop=True)

        return df

    def _open_site_filters(self, *, mask, analysis):
        key = mask, analysis
        try:
            return self._cache_site_filters[key]
        except KeyError:
            path = f"{self._path}/v3/site_filters/{analysis}/{mask}/"
            store = SafeStore(self._fs.get_mapper(path))
            root = zarr.open_consolidated(store=store)
            self._cache_site_filters[key] = root
            return root

    def site_filters(self, contig, mask, field="filter_pass", analysis="dt_20200416"):
        """Access SNP site filters.

        Parameters
        ----------
        contig : str
            Chromosome arm, e.g., "3R".
        mask : {"gamb_colu_arab", "gamb_colu", "arab"}
            Mask to use.
        field : str, optional
            Array to access.
        analysis : str, optional
            Site filters analysis version.

        Returns
        -------
        d : dask.array.Array

        """

        root = self._open_site_filters(mask=mask, analysis=analysis)
        z = root[contig]["variants"][field]
        d = da.from_array(z, chunks=z.chunks)
        return d

    def _open_snp_sites(self):
        if self._cache_snp_sites is None:
            path = f"{self._path}/v3/snp_genotypes/all/sites/"
            store = SafeStore(self._fs.get_mapper(path))
            root = zarr.open_consolidated(store=store)
            self._cache_snp_sites = root
        return self._cache_snp_sites

    def snp_sites(self, contig, field=None, site_mask=None, site_filters="dt_20200416"):
        """Access SNP site data (positions and alleles).

        Parameters
        ----------
        contig : str
            Chromosome arm, e.g., "3R".
        field : {"POS", "REF", "ALT"}, optional
            Array to access. If not provided, all three arrays POS, REF, ALT will be returned as a tuple.
        site_mask : {"gamb_colu_arab", "gamb_colu", "arab"}
            Site filters mask to apply.
        site_filters : str
            Site filters analysis version.

        Returns
        -------
        d : dask.array.Array or tuple of dask.array.Array

        """

        if field is None:
            # return POS, REF, ALT
            ret = tuple(
                self.snp_sites(contig=contig, field=f, site_mask=None)
                for f in ("POS", "REF", "ALT")
            )

        else:
            root = self._open_snp_sites()
            z = root[contig]["variants"][field]
            ret = da.from_array(z, chunks=z.chunks)

        if site_mask is not None:
            filter_pass = self.site_filters(
                contig=contig, mask=site_mask, analysis=site_filters
            ).compute()
            if isinstance(ret, tuple):
                ret = tuple(da.compress(filter_pass, d, axis=0) for d in ret)
            else:
                ret = da.compress(filter_pass, ret, axis=0)

        return ret

    def _open_snp_genotypes(self, *, sample_set):
        try:
            return self._cache_snp_genotypes[sample_set]
        except KeyError:
            release = self._lookup_release(sample_set=sample_set)
            path = f"{self._path}/{release}/snp_genotypes/all/{sample_set}/"
            store = SafeStore(self._fs.get_mapper(path))
            root = zarr.open_consolidated(store=store)
            self._cache_snp_genotypes[sample_set] = root
            return root

    def snp_genotypes(
        self,
        contig,
        sample_sets="v3_wild",
        field="GT",
        site_mask=None,
        site_filters="dt_20200416",
    ):
        """Access SNP genotypes and associated data.

        Parameters
        ----------
        contig : str
            Chromosome arm, e.g., "3R".
        sample_sets : str or list of str
            Can be a sample set identifier (e.g., "AG1000G-AO") or a list of sample set
            identifiers (e.g., ["AG1000G-BF-A", "AG1000G-BF-B"]) or a release identifier (e.g.,
            "v3") or a list of release identifiers.
        field : {"GT", "GQ", "AD", "MQ"}
            Array to access.
        site_mask : {"gamb_colu_arab", "gamb_colu", "arab"}
            Site filters mask to apply.
        site_filters : str, optional
            Site filters analysis version.

        Returns
        -------
        d : dask.array.Array

        """

        sample_sets = self._prep_sample_sets_arg(sample_sets=sample_sets)

        if isinstance(sample_sets, str):
            # single sample set
            root = self._open_snp_genotypes(sample_set=sample_sets)
            z = root[contig]["calldata"][field]
            d = da.from_array(z, chunks=z.chunks)

        else:
            # concatenate multiple sample sets
            ds = [
                self.snp_genotypes(contig=contig, sample_sets=c, field=field)
                for c in sample_sets
            ]
            d = da.concatenate(ds, axis=1)

        if site_mask is not None:
            filter_pass = self.site_filters(
                contig=contig, mask=site_mask, analysis=site_filters
            ).compute()
            d = da.compress(filter_pass, d, axis=0)

        return d

    def _open_genome(self):
        if self._cache_genome is None:
            path = f"{self._path}/reference/genome/agamp4/Anopheles-gambiae-PEST_CHROMOSOMES_AgamP4.zarr"
            store = SafeStore(self._fs.get_mapper(path))
            self._cache_genome = zarr.open_consolidated(store=store)
        return self._cache_genome

    def genome_sequence(self, contig):
        """Access the reference genome sequence.

        Parameters
        ----------
        contig : str
            Chromosome arm, e.g., "3R".

        Returns
        -------
        d : dask.array.Array

        """
        genome = self._open_genome()
        z = genome[contig]
        d = da.from_array(z, chunks=z.chunks)
        return d

    def _read_geneset(self):
        if self._cache_geneset is None:
            path = f"{self._path}/{gff3_path}"
            with self._fs.open(path, mode="rb") as f:
                self._cache_geneset = read_gff3(f, compression="gzip")
        return self._cache_geneset

    def geneset(self, attributes=("ID", "Parent", "Name")):
        """Access genome feature annotations (AgamP4.12).

        Parameters
        ----------
        attributes : list of str, optional
            Attribute keys to unpack into columns. Provide "*" to unpack all attributes.

        Returns
        -------
        df : pandas.DataFrame

        """

        df = self._read_geneset()
        if attributes is not None:
            df = unpack_gff3_attributes(df, attributes=attributes)

        return df

    def is_accessible(self, contig, site_mask, site_filters="dt_20200416"):
        """Compute genome accessibility array.

        Parameters
        ----------
        contig : str
            Chromosome arm, e.g., "3R".
        site_mask : {"gamb_colu_arab", "gamb_colu", "arab"}
            Site filters mask to apply.
        site_filters : str, optional
            Site filters analysis version.

        Returns
        -------
        a : numpy.ndarray

        """

        # determine contig sequence length
        seq_length = self.genome_sequence(contig).shape[0]

        # setup output
        is_accessible = np.zeros(seq_length, dtype=bool)

        # access positions
        pos = self.snp_sites(contig, field="POS").compute()

        # access site filters
        filter_pass = self.site_filters(
            contig, mask=site_mask, analysis=site_filters
        ).compute()

        # assign values from site filters
        is_accessible[pos - 1] = filter_pass

        return is_accessible

    def snp_effects(self, transcript, site_mask, chop):

        # take an AGAP transcript ID and get meta data from the gff using veff
        if self._cache_annotator is None:
            self._cache_annotator = veff.Annotator(
                genome=self._open_genome(),
                gff3_path=_path_to_url(self._fs, self._path, gff3_path),
            )

        ann = self._cache_annotator
        feature = ann.get_feature(transcript)
        contig = feature[0]
        start = feature[3]
        stop = feature[4]
        strand = feature[6]

        print(
            f"transcript : {transcript}\nchromosome : {contig} \nstart : {start}\nstop : {stop}"
            f"\nstrand : {strand}"
        )

        # grab pos, ref and alt
        sites = self.snp_sites(contig=contig, site_mask=site_mask)

        # sites are dask arrays, turn pos into sorted index
        pos = allel.SortedIndex(sites[0].compute())
        # locate transcript range
        loc = pos.locate_range(start, stop)

        ref = sites[1][loc].compute()
        alt = sites[2][loc].compute()

        # build an initial dataframe with contig, pos, ref, alt columns
        df_in = pandas.DataFrame()
        df_in["position"] = np.asarray(pos[loc])
        df_in["ref_allele"] = [q.tobytes().decode() for q in np.asarray(ref)]
        # bytes within lists within lists...
        df_in["alt_alleles"] = [list(q.tobytes().decode()) for q in list(alt)]
        # explode the alt alleles into their own rows
        df_effects = df_in.explode("alt_alleles").reset_index(drop=True)

        # then, iterate over rows of the dataframe, calling get_effects()
        # for each row, and using that to build additional columns effect,
        # impact, etc.
        # df_effects # pandas dataframe with additional columns
        df_effects = df_effects[:chop]
        leffect = []
        if chop > 0:
            for row in df_effects.itertuples(index=True):
                for effect in ann.get_effects(
                    chrom=contig,
                    pos=row.position,
                    ref=row.ref_allele,
                    alt=row.alt_alleles,
                    transcript_ids=[transcript],
                ):
                    leffect.append(effect.effect)
            df_effects["effect"] = leffect

        return df_effects

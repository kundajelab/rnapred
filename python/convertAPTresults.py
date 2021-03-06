import sys
import argparse
import numpy as np
import re
import rpy2.robjects.lib.ggplot2 as ggplot2
import rpy2.robjects as ro
from rpy2.robjects.packages import importr
import os
import os.path
import pandas as pd

def transcript_ids_to_genes(annot_file):
    """Reads an exon array annotation file and maps transcript 
    cluster ids to gene names.

    Args:
    - annot_file: Annotation file (csv)

    Return value:
    A tuple (gene_idx, genes, id_to_gene), where:
    - gene_idx is a dictionary mapping each unique gene name to
    an index (useful for downstream analyses).
    - genes is an array of unique gene names in the annotation file.
    - id_to_gene is a dictionary mapping transcript cluster ids
    to gene names.
    """

    id_to_gene = {}
    genes = []
    gene_idx = {}

    with open(annot_file, 'r') as infile:
        for line in infile:
            if line[0] == '#':
                continue
            # csv fields are all surrounded by quotes
            fields = [s.strip('"') for s in line.strip().split(',')]
            transcript_id = fields[0]
            if transcript_id.startswith('transcript'):
                continue
            # Different gene assignments for the same transcript
            # cluster id are separated by ///
            # For each of these assignments, there are several 
            # annotations (eg. refseq name, common name etc),
            # separated by //.
            gene_info = fields[7].split('///')
            gene_ids = []
            for gi in gene_info:
                if len(gi.split('//')) > 1:
                    g = gi.split('//')[1].strip()
                    if not g in gene_ids:
                        gene_ids.append(g)
                    if not g in gene_idx:
                        gene_idx[g] = len(genes)
                        genes.append(g)
            if len(gene_ids) > 0:
                id_to_gene[transcript_id] = gene_ids
    assert(len(genes) == len(gene_idx))
    return (gene_idx, genes, id_to_gene)


def read_affy_summary(affy_summary, genes, gene_idx, id_to_gene):
    """Reads the summary produced by Affymetrix Power Tools and 
    produces a matrix with the average expression levels across
    all transcript cluster ids for the same gene.

    Args:
    - affy_summary: Output of APT
    - genes, gene_idx, id_to_gene: As in transcript_ids_to_genes.

    Return value:
    A tuple with the following:
    - expr: Expression matrix genes x cel_files
    - genes: Names of genes in the matrix. This contains only
    the elements of the input genes list that appeared in the 
    affy file.
    - cel_names: Names of cel files corresponding to the columns 
    of the expression matrix.
    """

    ngenes = len(genes)
    # Number of transcript cluster ids per gene.
    rows_per_gene = np.zeros((ngenes,), dtype = np.int)

    with open(affy_summary, 'r') as infile:
        for line in infile:
            if line[0] == '#':
                continue
            fields = line.strip().split()
            if fields[0] == 'probeset_id':
                cel_names = [re.sub('.CEL', '', f) for f in fields[1:]]
                ncel = len(cel_names)
                expr = np.zeros((ngenes, ncel), dtype = np.float)
            else:
                if not fields[0] in id_to_gene:
                    # This can happen if the id did not map to a known gene.
                    continue
                gene_list = id_to_gene[fields[0]]
                for g in gene_list:
                    tmp_gene_idx = gene_idx[g]
                    expr[tmp_gene_idx, :] = expr[tmp_gene_idx, :] + np.array(fields[1:], dtype = np.float)
                    rows_per_gene[tmp_gene_idx] += 1
    
    good_rows = rows_per_gene > 0
    out_genes = [g[0] for g in zip(genes, list(good_rows)) if g[1]]
    expr = expr[good_rows, :]
    expr = expr.T / rows_per_gene[good_rows]
    expr = expr.T
    return (expr, out_genes, cel_names)


def plot_cels(expr, expt_names, expt_name_idx, cel_names, outdir = None):
    """Makes correlation plots between CEL files for the same cell type"""

    fsize = 10
    names_1 = []
    names_2 = []
    cors = []
    titles = []
    
    for ex_idx, ex in enumerate(expt_names):
        # Indices of CEL files (columns of expr) corresponding to that cell type
        tmp_idx = expt_name_idx[ex]
        plot_idx = 0
        
        for i in range(len(tmp_idx)):
            name1 = re.sub('_', '.', cel_names[tmp_idx[i]])
            for j in range(i + 1, len(tmp_idx)):
                name2 = re.sub('_', '.', cel_names[tmp_idx[j]])
                plot_idx += 1
                cor = np.corrcoef(expr[:, tmp_idx[i]], expr[:, tmp_idx[j]])[0, 1]
                names_1.append(name1)
                names_2.append(name2)
                cors.append(cor)
                titles.append(ex + '-' + str(plot_idx))
                
                df = ro.DataFrame({'x':ro.FloatVector(expr[:, tmp_idx[i]]), 
                                   'y':ro.FloatVector(expr[:, tmp_idx[j]])})
                gp = ggplot2.ggplot(df) + ggplot2.aes_string(x = 'x', y = 'y') + \
                ggplot2.geom_point(size = 1) + \
                ggplot2.scale_x_continuous(name1) + ggplot2.scale_y_continuous(name2) + \
                ggplot2.theme_bw() + ggplot2.ggtitle('{:s}-{:d} ({:.4f})'.format(ex, plot_idx, cor)) + \
                ggplot2.theme(**{'axis.text.x':ggplot2.element_text(size = fsize),
                                 'axis.title.x':ggplot2.element_text(size = 8),
                                 'axis.text.y':ggplot2.element_text(size = fsize),
                                 'axis.title.y':ggplot2.element_text(size = 8, angle = 90),
                                 'plot.title':ggplot2.element_text(size = fsize)})
                
                if outdir is None:
                    gp.plot()
                else:
                    if not os.path.isdir(outdir):
                        os.makedirs(outdir)
                    outfile = os.path.join(outdir, ex + '-' + str(plot_idx) + '.png')
                    ro.r.ggsave(filename = outfile, plot = gp, width = 85, height = 85, unit = 'mm')
    df = pd.DataFrame({'name1':names_1, 'name2':names_2, 'cor':cors}, index = titles)
    if not outdir is None:
        df.to_csv(os.path.join(outdir, 'cor_summary.txt'), sep = '\t')
    return df


def get_expt_map(cel_map_file, cel_names):
    expt_names = []
    # Maps each condition to a list of indices of columns of expr
    expt_name_idx = {}
    with open(cel_map_file, 'r') as infile:
        for line in infile:
            fields = line.strip().split()
            cel = re.sub('.CEL', '', fields[0])
            expt = fields[1]
            if expt == '-':
                continue
            cel_idx = np.argwhere(np.array(cel_names) == cel)[0][0]
            if not expt in expt_name_idx:
                expt_names.append(expt)
                expt_name_idx[expt] = []
            expt_name_idx[expt].append(cel_idx)
    return (expt_names, expt_name_idx)


def average_expt_cels(cel_map_file, expr, cel_names, outdir = None):
    """Reads a gene expression matrix and averages columns
    corresponding to the same condition.

    Args:
    - cel_map_file: File mapping CEL names to conditions.
    - expr: gene expression matrix, rows are genes, columns
    are CEL files.
    - cel_names: Names of columns of the expr.

    Return value:
    A tuple (expr_out, expt_names). expr_out has the same 
    number of rows as expr. expt_names are the names of the
    conditions corresponding to the columns of expr_out.
    """
    
    (expt_names, expt_name_idx) = get_expt_map(cel_map_file, cel_names)
    if not outdir is None:
        plot_cels(expr, expt_names, expt_name_idx, cel_names, outdir)

    nexpt = len(expt_names)
    expr_out = np.zeros((expr.shape[0], nexpt))
    for ex_idx, ex in enumerate(expt_names):
        expr_out[:, ex_idx] = np.mean(expr[:, expt_name_idx[ex]], axis = 1)

    return (expr_out, expt_names, expt_name_idx)


def main():
    desc = """Takes the output of Affymetrix Power Tools and 
averages values for the same gene across transcript cluster ids.
Then, it averages CELs that correspond to the same condition.

The following results are written in an npz file:
- expr: expression matrix, rows are genes, columns are conditions.
- expt_names: names of conditions corresponding to the columns of 
the expression matrix.
- gene_names.txt: names of genes corresponding to the rows
of the expression matrix."""
    
    parser = argparse.ArgumentParser(description = desc, 
                                     formatter_class = argparse.RawTextHelpFormatter)
    parser.add_argument('affy_output', 
                        help = 'Output of Affymetrix Power Tools')
    parser.add_argument('annot_file', 
                        help = 'CSV with transcript cluster id annotations')
    parser.add_argument('cel_map_file', 
                        help = 'File mapping CEL files to cell types or conditions')
    parser.add_argument('outfile', help = 'Output npz file')
    parser.add_argument('-o', '--plotdir', default = None,
                        help = 'Directory for correlation plots')
    args = parser.parse_args()

    (gene_idx, genes, id_to_gene) = transcript_ids_to_genes(args.annot_file)

    (expr, genes, cel_names) = read_affy_summary(args.affy_output, genes, 
                                                 gene_idx, id_to_gene)
 
    (expr_out, expt_names, expt_name_idx) = average_expt_cels(args.cel_map_file, 
                                                              expr, cel_names,
                                                              args.plotdir)
            
    np.savez(args.outfile, expr = expr_out, expt_names = expt_names, 
             gene_names = genes)


if __name__ == '__main__':
    main()
